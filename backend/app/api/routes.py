"""HTTP routes: streaming chat, sessions, schema, and suggestions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent import graph as agent_graph
from app.agent.prompts import schema_card
from app.agent.state import Intent
from app.api import sessions
from app.api.sse import frame
from app.config import get_settings
from app.models.schemas import (
    ChatRequest,
    ClarificationEvent,
    DoneEvent,
    ErrorCode,
    ErrorEvent,
    MessageList,
    Phase,
    PipelineEvent,
    RowsEvent,
    SessionCreated,
    StatusEvent,
    Suggestion,
    SuggestionList,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# Which node completing corresponds to which user-visible phase.
_NODE_PHASE = {
    "understand": Phase.UNDERSTANDING,
    "ground": Phase.GROUNDING,
    "generate": Phase.GENERATING,
    "validate": Phase.VALIDATING,
    "execute": Phase.EXECUTING,
}

_PHASE_DETAIL = {
    Phase.UNDERSTANDING: "Reading the question",
    Phase.GROUNDING: "Matching names to the data",
    Phase.GENERATING: "Writing the query",
    Phase.VALIDATING: "Checking the query is safe",
    Phase.EXECUTING: "Running the query",
    Phase.SYNTHESIZING: "Writing the answer",
}


async def _stream_answer(request: ChatRequest) -> AsyncIterator[dict[str, str]]:
    started = time.perf_counter()
    session = sessions.ensure(request.session_id)
    session_id = session["session_id"]

    sessions.add_message(session_id, "user", request.question)

    state = agent_graph.initial_state(session_id, request.question)
    # Everything before the turn just added.
    state["history"] = sessions.history(session_id)[:-1]

    graph = agent_graph.get_graph()
    config = {"configurable": {"thread_id": session_id}}

    final: dict[str, Any] = {}
    emitted_pipeline: list[Any] | None = None
    # Per-node wall time. Without this, "it is slow" cannot be attributed to a
    # node, and the three model calls look like one opaque wait.
    node_ms: dict[str, int] = {}
    node_started = time.perf_counter()

    deadline_s = get_settings().question_deadline_s

    try:
        yield frame(
            "status",
            StatusEvent(phase=Phase.UNDERSTANDING, detail=_PHASE_DETAIL[Phase.UNDERSTANDING]),
        )

        # SC-004's hard ceiling. The graph's own deadline check only gates repair
        # attempts, so without this a single slow generate-execute-synthesize
        # chain could run past it unbounded.
        async with asyncio.timeout(deadline_s):
            async for update in graph.astream(state, config=config, stream_mode="updates"):
                for node, payload in update.items():
                    now = time.perf_counter()
                    node_ms[node] = node_ms.get(node, 0) + int((now - node_started) * 1000)
                    node_started = now

                    if not isinstance(payload, dict):
                        continue
                    final.update(payload)

                    # Announce the phase the graph is entering next.
                    phase = _NODE_PHASE.get(node)
                    if phase is not None:
                        yield frame("status", StatusEvent(phase=phase, detail=_PHASE_DETAIL[phase]))

                    # The validated pipeline, before it runs (FR-021).
                    if node == "validate" and not payload.get("validation_errors"):
                        pipeline = payload.get("pipeline") or []
                        if pipeline and pipeline != emitted_pipeline:
                            emitted_pipeline = pipeline
                            yield frame(
                                "pipeline",
                                PipelineEvent(
                                    pipeline=pipeline,
                                    explanation=final.get("explanation", ""),
                                    attempt=final.get("attempt", 0) + 1,
                                ),
                            )

                    if node == "execute" and "rows" in payload:
                        yield frame(
                            "rows",
                            RowsEvent(
                                rows=payload.get("rows", []),
                                row_count=payload.get("row_count", 0),
                                truncated=payload.get("truncated", False),
                                elapsed_ms=payload.get("elapsed_ms", 0),
                            ),
                        )

        # A clarifying question ends the stream in place of an answer.
        # Compared with == rather than `is`: Intent is a StrEnum, and state that
        # has been through a checkpointer comes back as a plain string, which an
        # identity check would silently fail.
        clarification = final.get("clarification")
        if final.get("intent") == Intent.AMBIGUOUS and clarification:
            yield frame(
                "clarification",
                ClarificationEvent(
                    question=clarification["question"],
                    candidates=clarification.get("candidates", []),
                ),
            )

        answer = final.get("answer", "")
        if answer:
            yield frame(
                "status", StatusEvent(phase=Phase.SYNTHESIZING, detail="Writing the answer")
            )
            for chunk in _chunks(answer):
                yield frame("token", {"text": chunk})

        chart = final.get("chart_spec")
        if chart:
            yield frame("chart", chart)

        derivation = {
            "pipeline": final.get("pipeline", []),
            "row_count": final.get("row_count", 0),
            "truncated": final.get("truncated", False),
            "elapsed_ms": final.get("elapsed_ms", 0),
            "attempts": final.get("attempt", 0) + 1,
        }
        message = sessions.add_message(session_id, "assistant", answer, derivation)

        total_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "chat.answered",
            session=session_id,
            question=request.question,
            resolved=final.get("resolved_question"),
            intent=str(final.get("intent", "")),
            attempts=derivation["attempts"],
            rows=derivation["row_count"],
            total_ms=total_ms,
            node_ms=node_ms,
            mongo_ms=derivation["elapsed_ms"],
            model_ms=total_ms - derivation["elapsed_ms"],
        )

        yield frame(
            "done",
            DoneEvent(
                session_id=session_id,
                message_id=message["message_id"],
                answer=answer,
                total_ms=total_ms,
            ),
        )

    except TimeoutError:
        log.warning("chat.deadline_exceeded", session=session_id, seconds=deadline_s)
        yield frame(
            "error",
            ErrorEvent(
                code=ErrorCode.DEADLINE_EXCEEDED,
                message=(
                    f"That question took longer than {deadline_s} seconds, so I stopped. "
                    "A narrower question usually answers faster."
                ),
                recoverable=True,
            ),
        )
    except Exception as exc:
        log.exception("chat.failed", error=str(exc))
        yield frame(
            "error",
            ErrorEvent(code=ErrorCode.INTERNAL_ERROR, message=str(exc), recoverable=True),
        )


def _chunks(text: str, size: int = 24) -> list[str]:
    """Split the answer for streaming delivery."""
    words = text.split(" ")
    out: list[str] = []
    buffer = ""
    for word in words:
        buffer = f"{buffer} {word}".strip() if buffer else word
        if len(buffer) >= size:
            out.append(buffer + " ")
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


@router.post("/chat")
async def chat(request: ChatRequest) -> EventSourceResponse:
    return EventSourceResponse(_stream_answer(request))


@router.post("/sessions", response_model=SessionCreated, status_code=201)
async def create_session() -> SessionCreated:
    session = sessions.create()
    return SessionCreated(session_id=session["session_id"], created_at=session["created_at"])


@router.get("/sessions/{session_id}/messages", response_model=MessageList)
async def get_messages(session_id: str) -> MessageList:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return MessageList(session_id=session_id, messages=session["messages"])


@router.get("/schema")
async def get_schema() -> dict[str, Any]:
    card = schema_card()
    if not card:
        raise HTTPException(status_code=503, detail="schema card unavailable")
    return card


@router.get("/suggestions", response_model=SuggestionList)
async def get_suggestions() -> SuggestionList:
    """Starter questions, so a first-time user can ask something useful (SC-009)."""
    return SuggestionList(
        suggestions=[
            Suggestion(text="How many orders were created in Q3 2014?", category="counting"),
            Suggestion(text="Which quarter had the highest spending?", category="ranking"),
            Suggestion(text="What are the most frequently ordered line items?", category="ranking"),
            Suggestion(
                text="Which departments spent the most on IT services?", category="aggregation"
            ),
            Suggestion(text="Who are the top 10 suppliers by total spend?", category="aggregation"),
            Suggestion(text="Show monthly spending in 2014.", category="time_series"),
        ]
    )
