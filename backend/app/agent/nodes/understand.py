"""Resolve the question and classify intent.

Rewriting an elliptical follow-up into a standalone question here means every
downstream node has one input shape regardless of whether the turn was the
first or the fifth. It also makes a follow-up failure attributable: the
rewritten question is inspectable, so resolution errors are distinguishable
from generation errors.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import structlog

from app.agent import llm
from app.agent.prompts import UNDERSTAND_SYSTEM
from app.agent.state import AgentState, Intent
from app.config import get_settings

log = structlog.get_logger(__name__)

MAX_HISTORY_TURNS = 6


_ROLE_NAMES = {"human": "user", "ai": "assistant"}


def _render_turn(turn: Any) -> str | None:
    """Render a turn whether it is a plain dict or a LangChain message.

    The `messages` field carries LangGraph's `add_messages` reducer, which
    converts dicts into message objects. Handling only dicts made the history
    render empty, so every follow-up looked like a standalone question and was
    classified ambiguous.
    """
    if isinstance(turn, dict):
        role, content = turn.get("role", "user"), turn.get("content", "")
    else:
        role = _ROLE_NAMES.get(getattr(turn, "type", ""), getattr(turn, "type", "user"))
        content = getattr(turn, "content", "")

    content = str(content).strip()
    return f"{role}: {content}" if content else None


def _history(state: AgentState) -> str:
    turns = state.get("history", [])[-MAX_HISTORY_TURNS:]
    rendered = [line for line in (_render_turn(turn) for turn in turns) if line]
    return "\n".join(rendered) if rendered else "(no previous turns)"


def _period_is_covered(period: dict[str, Any] | None) -> bool:
    """FR-015: a period wholly outside the data is stated, not queried."""
    if not period:
        return True

    settings = get_settings()
    try:
        start = date.fromisoformat(str(period.get("start"))[:10])
        end = date.fromisoformat(str(period.get("end"))[:10])
    except (TypeError, ValueError):
        return True

    # Covered if the requested window overlaps the data window at all.
    return not (end < settings.data_coverage_start or start > settings.data_coverage_end)


async def understand(state: AgentState) -> dict[str, Any]:
    question = state["question"]

    user_prompt = (
        f"Conversation so far:\n{_history(state)}\n\n"
        f"New message: {question}\n\n"
        f"Data covers {get_settings().data_coverage_start} to "
        f"{get_settings().data_coverage_end}."
    )

    try:
        parsed = await llm.complete_json(UNDERSTAND_SYSTEM, user_prompt, max_tokens=250)
    except llm.LLMUnavailable as exc:
        log.warning("understand.llm_failed", error=str(exc))
        # Degrade to treating the question as standalone data rather than
        # failing the turn outright.
        return {"resolved_question": question, "intent": Intent.DATA, "entity_matches": {}}

    resolved = str(parsed.get("resolved_question") or question).strip() or question
    raw_intent = str(parsed.get("intent") or "data").strip()

    try:
        intent = Intent(raw_intent)
    except ValueError:
        intent = Intent.DATA

    period = parsed.get("period")
    if intent is Intent.DATA and isinstance(period, dict) and not _period_is_covered(period):
        intent = Intent.PERIOD_NOT_COVERED

    entities = parsed.get("entities")
    named = entities if isinstance(entities, dict) else {}

    log.info(
        "understand",
        resolved=resolved,
        intent=intent.value,
        entities=json.dumps(named)[:200],
    )

    return {
        "resolved_question": resolved,
        "intent": intent,
        "entity_matches": {"named": named, "period": period},
    }
