"""Execute the validated pipeline, and decide whether to repair."""

from __future__ import annotations

import time
from typing import Any

import structlog
from pymongo.errors import ExecutionTimeout, PyMongoError

from app.agent.state import AgentState, deadline_exceeded
from app.config import get_settings

log = structlog.get_logger(__name__)


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Make a result row JSON-safe without losing meaning."""
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if key == "_id" and value is None:
            clean[key] = "total"
        elif hasattr(value, "isoformat"):
            clean[key] = value.isoformat()
        elif isinstance(value, dict | list):
            clean[key] = value
        else:
            clean[key] = value
    return clean


async def execute(state: AgentState) -> dict[str, Any]:
    from app.db.client import get_collection

    settings = get_settings()
    pipeline = state.get("pipeline", [])
    started = time.perf_counter()

    try:
        # PyMongo's async aggregate() is itself a coroutine returning the
        # cursor, so it must be awaited before iteration.
        cursor = await get_collection().aggregate(
            pipeline,
            maxTimeMS=settings.query_timeout_ms,
            allowDiskUse=True,
        )
        rows = [_serialize(row) async for row in cursor]
    except ExecutionTimeout as exc:
        log.warning("execute.timeout", error=str(exc))
        return {
            "validation_errors": [
                "the query exceeded the time limit — narrow the filter or aggregate more"
            ],
            "error": {"code": "query_timeout", "message": str(exc)},
        }
    except PyMongoError as exc:
        log.warning("execute.failed", error=str(exc))
        return {"validation_errors": [f"MongoDB rejected the pipeline: {exc}"]}

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    truncated = len(rows) >= settings.max_result_rows

    log.info("execute", rows=len(rows), elapsed_ms=elapsed_ms, truncated=truncated)

    return {
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "validation_errors": [],
        "error": None,
    }


async def repair(state: AgentState) -> dict[str, Any]:
    """Increment the attempt counter.

    The budget itself is enforced in one place — `should_repair` — so the
    3-attempt ceiling and the deadline cannot drift apart.
    """
    attempt = state.get("attempt", 0) + 1
    log.info("repair", attempt=attempt, errors=state.get("validation_errors"))
    return {"attempt": attempt}


def should_repair(state: AgentState) -> bool:
    """FR-008: at most 3 repairs, and never past the per-question deadline."""
    settings = get_settings()

    if not state.get("validation_errors"):
        return False
    if state.get("attempt", 0) >= settings.max_repair_attempts:
        return False
    return not deadline_exceeded(state)
