"""Terminal nodes: synthesis and the paths that answer without querying.

`synthesize` receives the result rows and the question — deliberately not the
schema card, not prior turns' data, not the vocabulary. Constitution Principle
III is therefore a property of what this node can see, rather than an
instruction it might disregard.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.agent import llm
from app.agent.prompts import SYNTHESIZER_SYSTEM, schema_card
from app.agent.state import AgentState

log = structlog.get_logger(__name__)

CHARTABLE_SHAPES = {"category_measure", "time_series"}
MIN_CHART_ROWS = 2
MAX_ROWS_IN_PROMPT = 60


def _empty_answer(state: AgentState) -> str:
    resolved = state.get("entity_matches", {}).get("resolved", {})
    filters = ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in resolved.items())
    suffix = f" (filters applied: {filters})" if filters else ""
    return (
        "No matching records were found for that question"
        f"{suffix}. The dataset may not contain data meeting those criteria."
    )


def _chart_spec(state: AgentState) -> dict[str, Any] | None:
    """FR-022: chart rankings and series, never a single scalar."""
    shape = state.get("expected_shape", "rows")
    rows = state.get("rows", [])

    if shape not in CHARTABLE_SHAPES or len(rows) < MIN_CHART_ROWS:
        return None

    keys = list(rows[0].keys())
    if len(keys) < 2:
        return None

    x_field = "_id" if "_id" in keys else keys[0]
    y_field = next(
        (k for k in keys if k != x_field and isinstance(rows[0][k], int | float)),
        None,
    )
    if y_field is None:
        return None

    return {
        "type": "line" if shape == "time_series" else "bar",
        "x_field": x_field,
        "y_field": y_field,
        "title": state.get("explanation", "") or state.get("resolved_question", ""),
    }


async def synthesize(state: AgentState) -> dict[str, Any]:
    rows = state.get("rows", [])

    # Zero rows short-circuits with no model call: there is nothing to describe,
    # and a model given an empty table is exactly where invention happens.
    if not rows:
        log.info("synthesize.empty")
        return {"answer": _empty_answer(state), "chart_spec": None}

    resolved = state.get("entity_matches", {}).get("resolved", {})
    shown = rows[:MAX_ROWS_IN_PROMPT]

    context = [
        f"Question: {state.get('resolved_question', '')}",
        f"Rows returned by the query: {state.get('row_count', 0)}",
    ]
    if resolved:
        context.append(f"Filters applied: {json.dumps(resolved)}")
    if state.get("truncated"):
        context.append(
            "NOTE: the query hit the result limit, so these are not all matching records."
        )

    # Being explicit about elision matters: handed a silent 60-of-90 slice, a
    # model will happily add it up and present the partial sum as a total.
    if len(rows) > len(shown):
        context.append(
            f"NOTE: you are being shown only the first {len(shown)} of "
            f"{len(rows)} rows. Do not describe the rows you cannot see, and do "
            f"not combine these into a total."
        )

    context.append(f"Results:\n{json.dumps(shown, default=str)}")

    try:
        answer = await llm.complete(SYNTHESIZER_SYSTEM, "\n".join(context), max_tokens=500)
    except llm.LLMUnavailable as exc:
        log.warning("synthesize.llm_failed", error=str(exc))
        answer = (
            f"The query returned {state.get('row_count', 0)} row(s), shown below. "
            "A written summary is unavailable because the language model could not be reached."
        )

    return {"answer": answer, "chart_spec": _chart_spec(state)}


async def answer_schema(state: AgentState) -> dict[str, Any]:
    """Describe the dataset without querying it."""
    card = schema_card()
    coverage = card.get("coverage", {})
    vocabularies = card.get("vocabularies", {})

    answer = (
        f"This dataset holds {card.get('document_count', 0):,} purchase order line items "
        f"from the State of California, covering {coverage.get('from')} to "
        f"{coverage.get('to')} ({', '.join(coverage.get('fiscal_years', []))}). "
        f"They make up {card.get('distinct_orders', 0):,} distinct purchase orders across "
        f"{len(vocabularies.get('acquisition_type', []))} acquisition types. "
        "Each record carries the ordering department, supplier, item, quantity, and pricing. "
        "You can ask about order counts, spending by period, department, supplier or "
        "category, and the most frequently ordered items."
    )
    return {"answer": answer, "rows": [], "chart_spec": None}


async def decline(state: AgentState) -> dict[str, Any]:
    """FR-005: out of scope, answered rather than errored."""
    return {
        "answer": (
            "I can only answer questions about the State of California large-purchases "
            "dataset — things like how many orders were placed in a period, which "
            "departments or suppliers spent the most, or which items were ordered most "
            "often. Ask me one of those and I will query the data."
        ),
        "rows": [],
        "chart_spec": None,
    }


async def period_not_covered(state: AgentState) -> dict[str, Any]:
    """FR-015: say the period is not covered, do not return an empty table."""
    coverage = schema_card().get("coverage", {})
    return {
        "answer": (
            f"That period is outside the data I have. This dataset covers "
            f"{coverage.get('from')} to {coverage.get('to')} — fiscal years "
            f"{', '.join(coverage.get('fiscal_years', []))}. "
            "Ask about a period inside that range and I can answer it."
        ),
        "rows": [],
        "chart_spec": None,
    }


async def ask_clarify(state: AgentState) -> dict[str, Any]:
    """FR-014a / FR-004: ask rather than guess."""
    clarification = state.get("clarification")
    if clarification:
        return {"answer": clarification["question"], "rows": [], "chart_spec": None}

    return {
        "answer": (
            "I'm not sure what you're asking for. Could you rephrase — for example, "
            "name the period, department, or category you have in mind?"
        ),
        "rows": [],
        "chart_spec": None,
    }


async def give_up(state: AgentState) -> dict[str, Any]:
    """FR-008: report failure rather than answering approximately."""
    errors = state.get("validation_errors") or []
    error = state.get("error") or {}
    log.warning("give_up", attempts=state.get("attempt", 0), errors=errors, code=error.get("code"))

    # An outage and an unanswerable question are different failures, and telling
    # the user to rephrase when the provider was never reached is misleading.
    if error.get("code") == "llm_unavailable":
        return {
            "answer": (
                "I couldn't reach the language model that turns your question into a "
                "database query, so I can't answer right now. This is a service or "
                "configuration problem on my side, not a problem with your question."
            ),
            "rows": [],
            "chart_spec": None,
            "error": {"code": "llm_unavailable", "message": error.get("message", "")},
        }

    detail = f" ({errors[0]})" if errors else ""
    return {
        "answer": (
            "I couldn't build a valid query for that question"
            f"{detail}. Try rephrasing it — naming a specific period, department, "
            "supplier, or category usually helps."
        ),
        "rows": [],
        "chart_spec": None,
        "error": {"code": "validation_failed", "message": "; ".join(errors) or "no valid query"},
    }
