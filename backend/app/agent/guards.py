"""Deterministic aggregation-pipeline validator.

Constitution Principle II: every LLM-generated pipeline passes through here
before it reaches the driver, and a rejection must not be bypassable by prompt
content. So this module contains no model call, no network access, and no
configuration a prompt could reach — just Python deciding on structure.

It is an allow-list. A stage MongoDB introduces in some future version is
rejected because it is not on the list, rather than permitted because nobody
remembered to add it to a deny-list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Stages the agent is permitted to emit. Everything the four named question
# types need, and nothing that writes, reaches another collection, or executes
# server-side JavaScript.
ALLOWED_STAGES: frozenset[str] = frozenset(
    {
        "$match",
        "$group",
        "$sort",
        "$limit",
        "$skip",
        "$project",
        "$addFields",
        "$set",
        "$count",
        "$unwind",
        "$bucket",
        "$bucketAuto",
        "$facet",
        "$sortByCount",
        "$sample",
        "$replaceRoot",
    }
)

# Named explicitly so a reader can see what is being defended against, and so a
# test can assert the documented set is covered. Redundant with the allow-list
# by construction — that redundancy is the point.
FORBIDDEN_STAGES: frozenset[str] = frozenset(
    {
        "$out",  # writes a collection
        "$merge",  # writes a collection
        "$where",  # server-side JavaScript
        "$function",  # server-side JavaScript
        "$accumulator",  # server-side JavaScript
        "$graphLookup",  # unbounded recursive traversal
        "$lookup",  # reaches another collection
        "$unionWith",  # reaches another collection
        "$currentOp",
        "$listSessions",
        "$listLocalSessions",
        "$planCacheStats",
        "$collStats",
        "$indexStats",
    }
)

# Operators that execute code or escape the collection, wherever they appear —
# including nested deep inside a $match or $group expression.
FORBIDDEN_OPERATORS: frozenset[str] = frozenset(
    {"$where", "$function", "$accumulator", "$out", "$merge", "$graphLookup", "$unionWith"}
)

MAX_STAGES = 20
MAX_DEPTH = 12
DEFAULT_ROW_LIMIT = 200

# Stages after which the output is already bounded, so no $limit is needed.
_TERMINAL_STAGES = frozenset({"$count"})

_SCHEMA_CARD = Path(__file__).resolve().parents[3] / "data_pipeline" / "schema_card.json"


class PipelineRejected(ValueError):
    """The pipeline is not permitted to run. Carries a repairable reason."""


def _load_known_fields() -> frozenset[str]:
    """Field names from the generated schema card.

    Falls back to an empty set, which disables field checking rather than
    rejecting everything — a missing schema card is a deployment problem, not a
    reason to refuse every question.
    """
    try:
        card = json.loads(_SCHEMA_CARD.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    return frozenset(field["name"] for field in card.get("fields", []))


KNOWN_FIELDS: frozenset[str] = _load_known_fields()

# Accumulators and computed names that legitimately appear as keys but are not
# document fields.
_RESERVED_KEYS = frozenset({"_id"})


def _walk_for_forbidden_operators(node: Any, depth: int = 0) -> None:
    """Recurse through an arbitrary expression looking for code execution."""
    if depth > MAX_DEPTH:
        raise PipelineRejected(f"expression nested deeper than {MAX_DEPTH} levels")

    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_OPERATORS:
                raise PipelineRejected(
                    f"operator {key} is not permitted — it executes code or reaches "
                    f"outside {'the target collection'}"
                )
            _walk_for_forbidden_operators(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_for_forbidden_operators(item, depth + 1)


def _collect_field_references(node: Any, found: set[str], depth: int = 0) -> None:
    """Gather field names referenced as `$field` or as a $match key."""
    if depth > MAX_DEPTH:
        return

    if isinstance(node, dict):
        for key, value in node.items():
            if not key.startswith("$") and key not in _RESERVED_KEYS:
                found.add(key.split(".")[0])
            _collect_field_references(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_field_references(item, found, depth + 1)
    elif isinstance(node, str) and node.startswith("$") and not node.startswith("$$"):
        found.add(node[1:].split(".")[0])


def _validate_stages(pipeline: list[Any], depth: int = 0) -> None:
    """Check stage names, including inside $facet sub-pipelines."""
    if depth > 3:
        raise PipelineRejected("$facet nested too deeply")

    for stage in pipeline:
        if not isinstance(stage, dict):
            raise PipelineRejected(f"pipeline stage must be an object, got {type(stage).__name__}")
        if len(stage) != 1:
            raise PipelineRejected(
                f"each stage must have exactly one operator, got {sorted(stage)}"
            )

        ((name, body),) = stage.items()

        if name in FORBIDDEN_STAGES:
            raise PipelineRejected(f"stage {name} is not permitted")
        if name not in ALLOWED_STAGES:
            raise PipelineRejected(
                f"stage {name} is not on the allow-list. Permitted: {sorted(ALLOWED_STAGES)}"
            )

        _walk_for_forbidden_operators(body)

        # A forbidden stage nested inside a permitted $facet is still forbidden.
        if name == "$facet" and isinstance(body, dict):
            for sub_pipeline in body.values():
                if isinstance(sub_pipeline, list):
                    _validate_stages(sub_pipeline, depth + 1)


def _stage_outputs(name: str, body: Any, available: set[str]) -> set[str]:
    """The field names visible to the stage *after* this one.

    A pipeline reshapes its documents as it goes: after `$group` the only fields
    that exist are `_id` and the accumulator names. Checking every stage against
    the original schema would reject the perfectly ordinary
    `$group` → `$sort` → `$project` shape, because `spend` and `orders` are not
    columns in the collection.
    """
    if name == "$group" and isinstance(body, dict):
        return set(body)

    if name == "$count":
        return {str(body)}

    if name in {"$addFields", "$set"} and isinstance(body, dict):
        return available | set(body)

    if name == "$project" and isinstance(body, dict):
        # Exclusion projections ({"x": 0}) narrow the existing set; inclusion
        # projections define a new one.
        including = [key for key, value in body.items() if value not in (0, False)]
        if not including:
            return available - set(body)
        return set(including) | {"_id"}

    if name == "$bucket" and isinstance(body, dict):
        return {"_id"} | set(body.get("output", {}))

    if name == "$sortByCount":
        return {"_id", "count"}

    if name == "$facet" and isinstance(body, dict):
        return set(body)

    if name == "$replaceRoot":
        # The document shape becomes whatever the expression produced; further
        # field checking would be guesswork, so stop constraining.
        return set()

    return available


def _validate_field_references(pipeline: list[Any]) -> None:
    if not KNOWN_FIELDS:
        return

    available: set[str] = set(KNOWN_FIELDS)
    unconstrained = False

    for stage in pipeline:
        ((name, body),) = stage.items()

        if unconstrained:
            break

        referenced: set[str] = set()
        if name in {"$group", "$project", "$addFields", "$set", "$facet", "$bucket"}:
            # The keys are output names; only the values reference fields.
            values = list(body.values()) if isinstance(body, dict) else body
            _collect_field_references(values, referenced)
        elif name in {"$count", "$limit", "$skip", "$sample"}:
            referenced = set()
        else:
            _collect_field_references(body, referenced)

        unknown = {field for field in referenced if field not in available}
        if unknown:
            raise PipelineRejected(
                f"unknown field(s) at stage {name}: {sorted(unknown)}. "
                f"Available at this point: {sorted(available)[:12]}…"
            )

        available = _stage_outputs(name, body, available)
        if not available:
            unconstrained = True


def _apply_row_bound(pipeline: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Ensure the result cannot exceed the row bound (FR-013)."""
    bounded = [dict(stage) for stage in pipeline]

    last_name = next(iter(bounded[-1]))
    if last_name in _TERMINAL_STAGES:
        return bounded

    for index, stage in enumerate(bounded):
        if "$limit" in stage:
            existing = stage["$limit"]
            if not isinstance(existing, int) or existing <= 0:
                raise PipelineRejected(f"$limit must be a positive integer, got {existing!r}")
            bounded[index] = {"$limit": min(existing, limit)}
            return bounded

    bounded.append({"$limit": limit})
    return bounded


def validate_pipeline(
    pipeline: Any,
    collection: str,
    *,
    target_collection: str = "purchase_orders",
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> list[dict[str, Any]]:
    """Validate and bound a pipeline, or raise PipelineRejected.

    Returns the pipeline that is safe to execute — which may differ from the
    input only by an injected or clamped `$limit`.
    """
    if collection != target_collection:
        raise PipelineRejected(
            f"queries are restricted to '{target_collection}', got '{collection}'"
        )

    if not isinstance(pipeline, list):
        raise PipelineRejected(f"pipeline must be a list, got {type(pipeline).__name__}")
    if not pipeline:
        raise PipelineRejected("pipeline is empty")
    if len(pipeline) > MAX_STAGES:
        raise PipelineRejected(f"pipeline has {len(pipeline)} stages, maximum is {MAX_STAGES}")

    _validate_stages(pipeline)
    _validate_field_references(pipeline)

    return _apply_row_bound(pipeline, row_limit)
