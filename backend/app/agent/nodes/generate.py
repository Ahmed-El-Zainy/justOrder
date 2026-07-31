"""Generate a pipeline, then validate it.

Generation and validation are separate nodes because they fail differently and
only one of them is allowed to admit a pipeline to the driver.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.agent import llm
from app.agent.guards import PipelineRejected, validate_pipeline
from app.agent.prompts import GENERATOR_SYSTEM, render_examples, render_schema
from app.agent.state import AgentState
from app.config import get_settings

log = structlog.get_logger(__name__)

TARGET_COLLECTION = "purchase_orders"


def _system_prompt() -> str:
    return GENERATOR_SYSTEM.format(schema=render_schema(), examples=render_examples())


def _user_prompt(state: AgentState) -> str:
    parts = [f"Question: {state['resolved_question']}"]

    resolved = state.get("entity_matches", {}).get("resolved", {})
    if resolved:
        parts.append(
            "These names were matched to the exact values stored in the database. "
            "Use them verbatim in your $match:\n"
            + "\n".join(f"  {field}: {value!r}" for field, value in resolved.items())
        )

    unresolved = state.get("entity_matches", {}).get("unresolved", [])
    if unresolved:
        parts.append(
            "These filters were named but do not appear in the data. Include them "
            "anyway so the result is honestly empty: " + ", ".join(unresolved)
        )

    errors = state.get("validation_errors") or []
    if errors:
        previous = state.get("pipeline") or []
        parts.append(
            "Your previous attempt was rejected. Fix it.\n"
            f"Previous pipeline: {json.dumps(previous)}\n"
            "Errors:\n" + "\n".join(f"  - {error}" for error in errors)
        )

    return "\n\n".join(parts)


async def generate_pipeline(state: AgentState) -> dict[str, Any]:
    attempt = state.get("attempt", 0)

    try:
        parsed = await llm.complete_json(_system_prompt(), _user_prompt(state), max_tokens=1500)
    except llm.LLMUnavailable as exc:
        return {
            "error": {"code": "llm_unavailable", "message": str(exc)},
            "validation_errors": [f"model unavailable: {exc}"],
        }

    pipeline = parsed.get("pipeline")
    log.info(
        "generate_pipeline",
        attempt=attempt + 1,
        stages=len(pipeline) if isinstance(pipeline, list) else 0,
    )

    return {
        "pipeline": pipeline if isinstance(pipeline, list) else [],
        "explanation": str(parsed.get("explanation") or ""),
        "expected_shape": str(parsed.get("expected_shape") or "rows"),
        "collection": str(parsed.get("collection") or TARGET_COLLECTION),
        "validation_errors": [],
    }


async def validate(state: AgentState) -> dict[str, Any]:
    """Constitution Principle II gate. Pure Python — no model in this path."""
    settings = get_settings()
    collection = state.get("collection", TARGET_COLLECTION)  # type: ignore[call-overload]

    try:
        safe = validate_pipeline(
            state.get("pipeline", []),
            collection,
            target_collection=TARGET_COLLECTION,
            row_limit=settings.max_result_rows,
        )
    except PipelineRejected as exc:
        log.warning("validate.rejected", reason=str(exc))
        return {"validation_errors": [str(exc)]}

    return {"pipeline": safe, "validation_errors": []}
