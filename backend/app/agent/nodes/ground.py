"""Ground names in the question against values actually stored.

Runs before generation so the model is handed the literal strings the database
holds, rather than guessing them and matching nothing.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.agent import vocabulary
from app.agent.state import AgentState, Intent
from app.agent.vocabulary import MatchKind

log = structlog.get_logger(__name__)

ON_DEMAND = {"supplier_name", "item_name"}
FIELD_ALIASES = {"item_name": "item_name_normalized"}


async def ground_entities(state: AgentState) -> dict[str, Any]:
    named = state.get("entity_matches", {}).get("named", {})
    if not named:
        return {}

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    for field, raw in named.items():
        if not isinstance(raw, str) or not raw.strip():
            continue

        lookup_field = FIELD_ALIASES.get(field, field)

        if field in ON_DEMAND:
            result = await vocabulary.resolve_on_demand(lookup_field, raw)
        else:
            result = vocabulary.resolve(lookup_field, raw)

        if result.kind in (MatchKind.EXACT, MatchKind.SINGLE) and result.value:
            resolved[lookup_field] = result.value
        elif result.kind is MatchKind.AMBIGUOUS:
            ambiguous.append(
                {
                    "field": lookup_field,
                    "query": raw,
                    "values": [c.value for c in result.candidates],
                }
            )
        else:
            # A named filter the data does not contain. Kept, so the query
            # returns empty honestly rather than the filter being dropped.
            unresolved.append(f"{field}={raw!r}")

    log.info(
        "ground_entities",
        resolved=len(resolved),
        ambiguous=len(ambiguous),
        unresolved=len(unresolved),
    )

    update: dict[str, Any] = {
        "entity_matches": {
            **state.get("entity_matches", {}),
            "resolved": resolved,
            "unresolved": unresolved,
        }
    }

    if ambiguous:
        update["intent"] = Intent.AMBIGUOUS
        update["clarification"] = {
            "question": _clarification_question(ambiguous),
            "candidates": [
                {"field": item["field"], "values": item["values"]} for item in ambiguous
            ],
        }

    return update


def _clarification_question(ambiguous: list[dict[str, Any]]) -> str:
    first = ambiguous[0]
    label = first["field"].replace("_normalized", "").replace("_", " ")
    options = ", ".join(f'"{value}"' for value in first["values"][:5])
    return (
        f'"{first["query"]}" matches several {label} values. Did you mean one of these: {options}?'
    )
