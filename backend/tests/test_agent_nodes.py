"""Agent node behaviour and the six graph invariants.

The invariants in contracts/agent-state.md are the executable form of
constitution Principles II and III. They are asserted here with a stubbed model,
so the suite runs offline and a passing run means the *structure* is right
rather than that one model happened to behave.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import llm
from app.agent.nodes import execute as execute_node
from app.agent.nodes import generate as generate_node
from app.agent.nodes import ground as ground_node
from app.agent.nodes import respond as respond_node
from app.agent.state import AgentState, Intent, new_state


def _state(**overrides: Any) -> AgentState:
    state = new_state("test-session", overrides.pop("question", "how many orders?"), 30)
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


class TestValidateGatesEverything:
    """Invariant 1: no pipeline reaches the driver without passing validation."""

    async def test_forbidden_pipeline_never_executes(self, fake_collection: Any) -> None:
        state = _state(pipeline=[{"$out": "stolen"}], collection="purchase_orders")

        result = await generate_node.validate(state)

        assert result["validation_errors"]
        assert fake_collection.pipelines == []

    async def test_valid_pipeline_passes_and_is_bounded(self) -> None:
        state = _state(
            pipeline=[{"$match": {"acquisition_type": "IT Goods"}}],
            collection="purchase_orders",
        )

        result = await generate_node.validate(state)

        assert result["validation_errors"] == []
        assert result["pipeline"][-1] == {"$limit": 200}

    async def test_execute_runs_exactly_what_validate_returned(self, fake_collection: Any) -> None:
        fake_collection.rows = [{"_id": "IT Goods", "spend": 1.0}]
        validated = await generate_node.validate(
            _state(pipeline=[{"$match": {"calcard": True}}], collection="purchase_orders")
        )

        await execute_node.execute(_state(pipeline=validated["pipeline"]))

        assert fake_collection.pipelines == [validated["pipeline"]]


class TestRepairBudget:
    """Invariants 3 and 4: at most 3 attempts, never past the deadline."""

    def test_repairs_while_under_budget(self) -> None:
        assert execute_node.should_repair(_state(validation_errors=["bad"], attempt=0))
        assert execute_node.should_repair(_state(validation_errors=["bad"], attempt=2))

    def test_stops_at_three(self) -> None:
        assert not execute_node.should_repair(_state(validation_errors=["bad"], attempt=3))
        assert not execute_node.should_repair(_state(validation_errors=["bad"], attempt=9))

    def test_stops_when_deadline_passed_even_with_attempts_left(self) -> None:
        expired = _state(validation_errors=["bad"], attempt=0)
        expired["deadline_at"] = time.monotonic() - 1

        assert not execute_node.should_repair(expired)

    def test_no_repair_without_errors(self) -> None:
        assert not execute_node.should_repair(_state(validation_errors=[], attempt=0))

    async def test_repair_increments_the_counter(self) -> None:
        assert (await execute_node.repair(_state(attempt=1)))["attempt"] == 2


class TestGroundingNeverGuesses:
    async def test_ambiguous_name_routes_to_clarification(self, loaded_vocabulary: None) -> None:
        state = _state(entity_matches={"named": {"department_name": "Corrections"}})

        result = await ground_node.ground_entities(state)

        assert result["intent"] is Intent.AMBIGUOUS
        assert "Corrections and Rehabilitation, Department of" in str(result["clarification"])

    async def test_resolvable_name_is_substituted_verbatim(self, loaded_vocabulary: None) -> None:
        state = _state(entity_matches={"named": {"acquisition_type": "it services"}})

        result = await ground_node.ground_entities(state)

        assert result["entity_matches"]["resolved"]["acquisition_type"] == "IT Services"
        assert "intent" not in result

    async def test_unknown_name_is_kept_so_the_result_is_honestly_empty(
        self, loaded_vocabulary: None
    ) -> None:
        state = _state(entity_matches={"named": {"department_name": "Ministry of Magic"}})

        result = await ground_node.ground_entities(state)

        assert result["entity_matches"]["resolved"] == {}
        assert result["entity_matches"]["unresolved"]


class TestSynthesisIsGrounded:
    """Invariant 2 and Principle III."""

    async def test_empty_rows_short_circuit_without_calling_the_model(
        self, stub_model: Any
    ) -> None:
        result = await respond_node.synthesize(_state(rows=[], row_count=0))

        assert "No matching records" in result["answer"]
        assert stub_model.text_calls == []

    async def test_synthesis_prompt_contains_the_rows_and_not_the_schema(
        self, stub_model: Any
    ) -> None:
        stub_model.text_responses = ["18,352 orders."]
        state = _state(
            rows=[{"orders": 18_352}], row_count=1, resolved_question="orders in Q3 2014?"
        )

        await respond_node.synthesize(state)

        _, user_prompt = stub_model.text_calls[0]
        assert "18352" in user_prompt.replace(",", "")
        # The schema card would give the model figures to invent from.
        assert "CRITICAL" not in user_prompt

    async def test_truncation_is_disclosed_to_the_model(self, stub_model: Any) -> None:
        stub_model.text_responses = ["Top results."]
        await respond_node.synthesize(
            _state(rows=[{"_id": "a", "n": 1}], row_count=200, truncated=True)
        )

        prompt = stub_model.text_calls[0][1].lower()
        assert "not all matching records" in prompt


class TestTerminalPathsAlwaysAnswer:
    """Invariant 5: every terminal path yields an answer, never silence."""

    @pytest.mark.parametrize(
        "node",
        [
            respond_node.decline,
            respond_node.period_not_covered,
            respond_node.ask_clarify,
            respond_node.give_up,
            respond_node.answer_schema,
        ],
    )
    async def test_node_produces_an_answer(self, node: Any) -> None:
        result = await node(_state())

        assert result["answer"].strip()
        assert result["rows"] == []

    async def test_period_not_covered_names_the_range(self) -> None:
        answer = (await respond_node.period_not_covered(_state()))["answer"]

        assert "2012" in answer and "2015" in answer

    async def test_give_up_distinguishes_an_outage_from_a_bad_question(self) -> None:
        outage = await respond_node.give_up(
            _state(error={"code": "llm_unavailable", "message": "402"})
        )
        unanswerable = await respond_node.give_up(_state(validation_errors=["unknown field"]))

        assert "reach the language model" in outage["answer"]
        assert outage["error"]["code"] == "llm_unavailable"
        assert "rephrasing" in unanswerable["answer"]


class TestProviderOutageIsNotRepairable:
    async def test_generate_reports_llm_unavailable(self, stub_model: Any) -> None:
        stub_model.json_responses = [llm.LLMUnavailable("402 out of credits")]

        result = await generate_node.generate_pipeline(_state())

        assert result["error"]["code"] == "llm_unavailable"

    def test_graph_routes_an_outage_straight_to_give_up(self) -> None:
        """Retrying a 402 three times cannot succeed and misreports the cause."""
        outage = _state(error={"code": "llm_unavailable", "message": "402"})

        assert agent_graph._after_generate(outage) == "give_up"
        assert agent_graph._after_generate(_state(error=None)) == "validate"


class TestGuardsAreOffline:
    """Invariant 6: the validator must be decidable without a network."""

    def test_guards_module_has_no_network_imports(self) -> None:
        import app.agent.guards as guards

        with open(guards.__file__) as handle:
            source = handle.read()
        for name in ("httpx", "requests", "openai", "urllib", "socket"):
            assert f"import {name}" not in source


class TestGraphShape:
    def test_every_documented_node_exists(self) -> None:
        nodes = set(agent_graph.build_graph().get_graph().nodes)

        for expected in (
            "understand",
            "ground",
            "generate",
            "validate",
            "execute",
            "repair",
            "synthesize",
            "give_up",
            "decline",
            "ask_clarify",
            "period_not_covered",
            "answer_schema",
        ):
            assert expected in nodes

    def test_intent_routing_covers_every_intent(self) -> None:
        for intent in Intent:
            destination = agent_graph._route_intent(_state(intent=intent))
            assert destination
