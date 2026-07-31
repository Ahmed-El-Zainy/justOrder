"""Agent graph assembly.

The validation and repair loop is expressed as real edges rather than an inline
try/except, as the constitution requires. That makes two things structural: no
path reaches `execute` without passing `validate`, and the repair budget lives
in exactly one predicate.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import execute as execute_node
from app.agent.nodes import generate as generate_node
from app.agent.nodes import ground as ground_node
from app.agent.nodes import respond as respond_node
from app.agent.nodes import understand as understand_node
from app.agent.state import AgentState, Intent, deadline_exceeded, new_state
from app.config import get_settings

log = structlog.get_logger(__name__)


def _route_intent(
    state: AgentState,
) -> Literal["ground", "answer_schema", "decline", "period_not_covered", "ask_clarify"]:
    intent = state.get("intent", Intent.DATA)
    return {
        Intent.DATA: "ground",
        Intent.SCHEMA: "answer_schema",
        Intent.OUT_OF_SCOPE: "decline",
        Intent.PERIOD_NOT_COVERED: "period_not_covered",
        Intent.AMBIGUOUS: "ask_clarify",
    }.get(intent, "ground")


def _after_grounding(state: AgentState) -> Literal["generate", "ask_clarify"]:
    """A name matching several stored values asks rather than guesses."""
    if state.get("intent") is Intent.AMBIGUOUS:
        return "ask_clarify"
    return "generate"


def _after_generate(state: AgentState) -> Literal["validate", "give_up"]:
    """A provider outage is not a repairable query error.

    Retrying a 402 or a rate limit three times cannot succeed, wastes the
    budget, and ends up reporting "no valid query" when the truth is that the
    model was never reached.
    """
    error = state.get("error") or {}
    if error.get("code") == "llm_unavailable":
        return "give_up"
    return "validate"


def _after_validate(state: AgentState) -> Literal["execute", "repair", "give_up"]:
    if not state.get("validation_errors"):
        return "execute"
    return "repair" if execute_node.should_repair(state) else "give_up"


def _after_execute(state: AgentState) -> Literal["synthesize", "repair", "give_up"]:
    if state.get("validation_errors"):
        return "repair" if execute_node.should_repair(state) else "give_up"

    # One repair is allowed on an empty result, to catch an over-narrow filter.
    # After that, emptiness is reported as the finding it may well be (FR-007).
    if not state.get("rows") and state.get("attempt", 0) == 0 and not deadline_exceeded(state):
        return "repair"

    return "synthesize"


def build_graph() -> Any:
    builder = StateGraph(AgentState)

    builder.add_node("understand", understand_node.understand)
    builder.add_node("ground", ground_node.ground_entities)
    builder.add_node("generate", generate_node.generate_pipeline)
    builder.add_node("validate", generate_node.validate)
    builder.add_node("execute", execute_node.execute)
    builder.add_node("repair", execute_node.repair)
    builder.add_node("synthesize", respond_node.synthesize)
    builder.add_node("answer_schema", respond_node.answer_schema)
    builder.add_node("decline", respond_node.decline)
    builder.add_node("period_not_covered", respond_node.period_not_covered)
    builder.add_node("ask_clarify", respond_node.ask_clarify)
    builder.add_node("give_up", respond_node.give_up)

    builder.add_edge(START, "understand")
    builder.add_conditional_edges("understand", _route_intent)
    builder.add_conditional_edges("ground", _after_grounding)

    builder.add_conditional_edges("generate", _after_generate)
    builder.add_conditional_edges("validate", _after_validate)
    builder.add_conditional_edges("execute", _after_execute)

    # The repair cycle: back to generation with the errors attached.
    builder.add_edge("repair", "generate")

    for terminal in (
        "synthesize",
        "answer_schema",
        "decline",
        "period_not_covered",
        "ask_clarify",
        "give_up",
    ):
        builder.add_edge(terminal, END)

    return builder.compile(checkpointer=MemorySaver())


_graph: Any | None = None


def get_graph() -> Any:
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def initial_state(session_id: str, question: str) -> AgentState:
    return new_state(session_id, question, get_settings().question_deadline_s)
