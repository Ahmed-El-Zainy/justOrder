"""Resolve names in a question against the values actually stored.

The largest single lever on query accuracy. A user types "Department of
Consumer Affairs"; the database holds "Consumer Affairs, Department of". A
model asked to guess the literal string produces the natural word order,
matches nothing, and the assistant confidently reports zero — a wrong answer
that looks exactly like a right one.

Resolving against real values first converts that failure into either a correct
query or an explicit clarifying question (FR-014a).

Matching is split from loading so the decision logic is testable without a
database.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import Any

import structlog
from rapidfuzz import fuzz, process

from app.config import get_settings

log = structlog.get_logger(__name__)

# Cached in memory: small enough to be worth it. Suppliers (24,732) and item
# names (>80,000) are looked up on demand against their indexes instead.
CACHED_FIELDS = (
    "department_name",
    "acquisition_type",
    "acquisition_method",
    "sub_acquisition_type",
    "fiscal_year",
    "creation_quarter_label",
)

ON_DEMAND_FIELDS = ("supplier_name", "item_name_normalized")

MAX_CANDIDATES = 5

# How far ahead the best candidate must be before it is substituted without
# asking. Below this the readings are treated as genuinely competing.
DECISIVE_MARGIN = 5.0

# Words that appear across most department and category names and therefore
# carry no discriminating signal. Left in, they dominate the similarity score:
# "Ministry of Magic" scores 85 against "Consumer Affairs, Department of" purely
# on the shared ", Department of", which would have the assistant offering four
# unrelated departments for a nonsense query.
FILLER_TOKENS = frozenset({"department", "dept", "of", "the", "and", "for", "a", "an"})

_PUNCTUATION = str.maketrans({ch: " " for ch in ",.&()-/'\""})


def _distinctive(text: str) -> str:
    """Reduce a name to the tokens that actually distinguish it."""
    tokens = text.translate(_PUNCTUATION).lower().split()
    kept = [token for token in tokens if token not in FILLER_TOKENS]
    # Never reduce to nothing: a name made only of filler keeps its own tokens.
    return " ".join(kept or tokens)


class MatchKind(StrEnum):
    EXACT = "exact"
    SINGLE = "single"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass(frozen=True)
class Candidate:
    value: str
    score: float
    count: int = 0


@dataclass(frozen=True)
class Resolution:
    field: str
    query: str
    kind: MatchKind
    candidates: list[Candidate] = dataclass_field(default_factory=list)

    @property
    def value(self) -> str | None:
        """The resolved value, when resolution was unambiguous."""
        if self.kind in (MatchKind.EXACT, MatchKind.SINGLE) and self.candidates:
            return self.candidates[0].value
        return None


# field -> {stored value: document count}
_vocabulary: dict[str, dict[str, int]] = {}


def match_value(
    query: str,
    candidates: dict[str, int],
    *,
    threshold: int,
    floor: int,
    field: str = "",
) -> Resolution:
    """Pure matching. `candidates` maps stored value to its document count.

    An exact case-insensitive match wins outright. A single candidate at or
    above `threshold` is substituted silently. Candidates between `floor` and
    `threshold` are returned together for the clarification path. Below
    `floor`, nothing matched.
    """
    cleaned = query.strip()
    if not cleaned or not candidates:
        return Resolution(field=field, query=query, kind=MatchKind.NONE)

    lowered = cleaned.lower()
    for value, count in candidates.items():
        if value.lower() == lowered:
            return Resolution(
                field=field,
                query=query,
                kind=MatchKind.EXACT,
                candidates=[Candidate(value=value, score=100.0, count=count)],
            )

    # Two scores, each doing a job the other cannot.
    #
    # The *distinctive* score (filler tokens stripped, token_set_ratio) decides
    # whether a candidate is in the running at all. It is what stops "Ministry
    # of Magic" matching four departments on their shared ", Department of".
    #
    # The *full* score (whole string, WRatio) then ranks the survivors, because
    # the filler carries real signal once noise is excluded: "Department of
    # Transportation" should prefer "Transportation, Department of" over
    # "Transportation Commission, California", and only the unstripped
    # comparison knows that.
    values = list(candidates)
    stripped = [_distinctive(value) for value in values]

    scored = process.extract(
        _distinctive(cleaned),
        stripped,
        scorer=fuzz.token_set_ratio,
        limit=MAX_CANDIDATES,
        score_cutoff=floor,
    )
    if not scored:
        return Resolution(field=field, query=query, kind=MatchKind.NONE)

    matches = [
        Candidate(
            value=values[index],
            score=fuzz.WRatio(cleaned, values[index]),
            count=candidates.get(values[index], 0),
        )
        for _, _, index in scored
    ]
    # Ties break toward the commoner value.
    matches.sort(key=lambda c: (-c.score, -c.count))

    best = matches[0]
    runner_up = matches[1] if len(matches) > 1 else None

    # One candidate clearly ahead of the field, and strong in its own right.
    if best.score >= threshold and (
        runner_up is None or best.score - runner_up.score >= DECISIVE_MARGIN
    ):
        return Resolution(field=field, query=query, kind=MatchKind.SINGLE, candidates=[best])

    # Otherwise several readings are plausible — asking beats guessing (FR-014a).
    return Resolution(field=field, query=query, kind=MatchKind.AMBIGUOUS, candidates=matches)


async def load() -> None:
    """Populate the in-memory vocabulary from the field_vocabulary collection."""
    from app.db.client import get_vocabulary_collection

    global _vocabulary
    loaded: dict[str, dict[str, int]] = {}

    cursor = get_vocabulary_collection().find(
        {"field": {"$in": list(CACHED_FIELDS)}},
        {"_id": 0, "field": 1, "value": 1, "count": 1},
    )
    async for entry in cursor:
        loaded.setdefault(entry["field"], {})[entry["value"]] = entry.get("count", 0)

    _vocabulary = loaded
    log.info(
        "vocabulary.loaded",
        fields=len(loaded),
        values=sum(len(values) for values in loaded.values()),
    )


def is_loaded() -> bool:
    return bool(_vocabulary)


def set_vocabulary(field: str, values: dict[str, int]) -> None:
    """Inject a vocabulary directly. Used by tests and by the on-demand path."""
    _vocabulary[field] = values


def known_values(field: str) -> dict[str, int]:
    return _vocabulary.get(field, {})


def resolve(field: str, query: str) -> Resolution:
    """Resolve a name against a cached vocabulary."""
    settings = get_settings()
    return match_value(
        query,
        _vocabulary.get(field, {}),
        threshold=settings.fuzzy_match_threshold,
        floor=settings.fuzzy_ambiguous_floor,
        field=field,
    )


async def resolve_on_demand(field: str, query: str, limit: int = 400) -> Resolution:
    """Resolve against a high-cardinality field without caching it.

    Narrows with an indexed prefix/substring match first, then scores only the
    shortlist. Holding 24,732 supplier names in memory to answer the occasional
    supplier question would not earn the space.
    """
    from app.db.client import get_collection

    settings = get_settings()
    cleaned = query.strip()
    if not cleaned:
        return Resolution(field=field, query=query, kind=MatchKind.NONE)

    escaped = "".join(ch if ch.isalnum() or ch.isspace() else f"\\{ch}" for ch in cleaned)
    cursor = await get_collection().aggregate(
        [
            {"$match": {field: {"$regex": escaped, "$options": "i"}}},
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ],
        maxTimeMS=settings.query_timeout_ms,
    )

    candidates: dict[str, int] = {}
    async for row in cursor:
        if row["_id"]:
            candidates[str(row["_id"])] = row["count"]

    return match_value(
        cleaned,
        candidates,
        threshold=settings.fuzzy_match_threshold,
        floor=settings.fuzzy_ambiguous_floor,
        field=field,
    )


def vocabulary_snapshot() -> dict[str, list[str]]:
    """Enum values for the schema card and the /api/schema endpoint."""
    return {field: sorted(values) for field, values in _vocabulary.items()}


def as_dict() -> dict[str, Any]:
    return {field: dict(values) for field, values in _vocabulary.items()}
