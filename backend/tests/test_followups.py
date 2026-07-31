"""Follow-up resolution (US2) and the grounding guarantees around it.

The rewritten standalone question is the inspectable artifact that makes a
follow-up failure attributable: if turn 3 is wrong, these tests say whether
resolution or generation was at fault.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.nodes import respond as respond_node
from app.agent.nodes import understand as understand_node
from app.agent.state import new_state
from app.api import sessions


class FakeMessage:
    """Shaped like a LangChain message, which is what add_messages produces."""

    def __init__(self, type_: str, content: str) -> None:
        self.type = type_
        self.content = content


def _state(history: list[Any], question: str = "what about Q2?") -> Any:
    state = new_state("s", question, 30)
    state["history"] = history
    return state


class TestHistoryRendering:
    def test_renders_plain_dicts(self) -> None:
        rendered = understand_node._history(
            _state([{"role": "user", "content": "spending in Q1 2014?"}])
        )

        assert "user: spending in Q1 2014?" in rendered

    def test_renders_langchain_style_messages(self) -> None:
        """add_messages converts dicts to objects; filtering on dict made the
        history render empty and every follow-up looked standalone."""
        rendered = understand_node._history(
            _state([FakeMessage("human", "spending in Q1 2014?"), FakeMessage("ai", "$10bn")])
        )

        assert "user: spending in Q1 2014?" in rendered
        assert "assistant: $10bn" in rendered

    def test_empty_history_is_explicit(self) -> None:
        assert understand_node._history(_state([])) == "(no previous turns)"

    def test_blank_turns_are_dropped(self) -> None:
        assert understand_node._history(_state([{"role": "user", "content": "  "}])) == (
            "(no previous turns)"
        )

    def test_only_recent_turns_are_kept(self) -> None:
        history = [{"role": "user", "content": f"q{i}"} for i in range(20)]

        rendered = understand_node._history(_state(history))

        assert "q19" in rendered
        assert "q0" not in rendered


class TestHistoryReachesTheModel:
    async def test_prior_turns_are_in_the_prompt(self, stub_model: Any) -> None:
        stub_model.json_responses = [
            {"resolved_question": "What was total spending in Q2 2014?", "intent": "data"}
        ]
        state = _state(
            [
                {"role": "user", "content": "What was total spending in Q1 2014?"},
                {"role": "assistant", "content": "$10,118,001,668.73"},
            ]
        )

        await understand_node.understand(state)

        _, prompt = stub_model.json_calls[0]
        assert "Q1 2014" in prompt

    async def test_resolved_question_is_recorded(self, stub_model: Any) -> None:
        stub_model.json_responses = [
            {"resolved_question": "What was total spending in Q2 2014?", "intent": "data"}
        ]

        result = await understand_node.understand(_state([]))

        assert result["resolved_question"] == "What was total spending in Q2 2014?"

    async def test_outage_degrades_to_the_literal_question(self, stub_model: Any) -> None:
        """Better to answer the question as typed than to fail the turn."""
        stub_model.json_responses = []

        result = await understand_node.understand(_state([], question="how many orders?"))

        assert result["resolved_question"] == "how many orders?"
        assert result["intent"] == "data"


class TestSessionStore:
    def test_history_excludes_nothing_and_preserves_order(self) -> None:
        created = sessions.create()
        sid = created["session_id"]
        sessions.add_message(sid, "user", "first")
        sessions.add_message(sid, "assistant", "reply")
        sessions.add_message(sid, "user", "second")

        history = sessions.history(sid)

        assert [turn["content"] for turn in history] == ["first", "reply", "second"]

    def test_derivation_is_kept_on_assistant_turns(self) -> None:
        sid = sessions.create()["session_id"]
        sessions.add_message(sid, "assistant", "answer", {"row_count": 4})

        assert sessions.get(sid)["messages"][-1]["derivation"]["row_count"] == 4

    def test_ensure_reuses_a_known_session(self) -> None:
        sid = sessions.create()["session_id"]

        assert sessions.ensure(sid)["session_id"] == sid

    def test_ensure_creates_one_for_an_unknown_id(self) -> None:
        assert sessions.ensure("does-not-exist")["session_id"] != "does-not-exist"


class TestSynthesisDoesNotInventTotals:
    """The failure this guards against produced a plausible, wrong headline
    figure that no row contained."""

    async def test_elision_is_disclosed_to_the_model(self, stub_model: Any) -> None:
        stub_model.text_responses = ["ok"]
        rows = [{"_id": f"dept-{i}", "spend": float(i)} for i in range(90)]

        await respond_node.synthesize(
            _state([], question="by department?") | {"rows": rows, "row_count": 90}
        )

        _, prompt = stub_model.text_calls[0]
        assert "first 60" in prompt
        assert "not combine these into a total" in prompt

    async def test_no_elision_note_when_all_rows_are_shown(self, stub_model: Any) -> None:
        stub_model.text_responses = ["ok"]
        rows = [{"_id": "a", "spend": 1.0}]

        await respond_node.synthesize(_state([]) | {"rows": rows, "row_count": 1})

        assert "first 60" not in stub_model.text_calls[0][1]

    @pytest.mark.parametrize("phrase", ["must be COPIED", "must not calculate"])
    def test_system_prompt_forbids_calculation(self, phrase: str) -> None:
        from app.agent.prompts import SYNTHESIZER_SYSTEM

        assert phrase.lower() in SYNTHESIZER_SYSTEM.lower()
