"""Typed state carried through the agent graph."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class Intent(StrEnum):
    DATA = "data"
    SCHEMA = "schema"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"
    PERIOD_NOT_COVERED = "period_not_covered"


class AgentState(TypedDict, total=False):
    session_id: str
    messages: Annotated[list[Any], add_messages]

    # Conversation history for follow-up resolution. Deliberately NOT the
    # `messages` field above: that carries the add_messages reducer, which
    # appends, so passing the full history each turn would duplicate it.
    history: list[dict[str, str]]

    question: str
    resolved_question: str
    intent: Intent

    entity_matches: dict[str, Any]
    clarification: dict[str, Any] | None

    pipeline: list[dict[str, Any]]
    explanation: str
    expected_shape: str
    validation_errors: list[str]

    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int

    attempt: int
    deadline_at: float

    answer: str
    chart_spec: dict[str, Any] | None
    error: dict[str, Any] | None


def new_state(session_id: str, question: str, deadline_s: int) -> AgentState:
    return AgentState(
        session_id=session_id,
        question=question,
        resolved_question=question,
        messages=[],
        history=[],
        entity_matches={},
        clarification=None,
        pipeline=[],
        validation_errors=[],
        rows=[],
        row_count=0,
        truncated=False,
        elapsed_ms=0,
        attempt=0,
        # Monotonic: immune to wall-clock adjustment mid-question.
        deadline_at=time.monotonic() + deadline_s,
        answer="",
        chart_spec=None,
        error=None,
    )


def deadline_exceeded(state: AgentState) -> bool:
    return time.monotonic() >= state.get("deadline_at", 0.0)


def time_remaining(state: AgentState) -> float:
    return max(0.0, state.get("deadline_at", 0.0) - time.monotonic())
