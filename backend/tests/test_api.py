"""HTTP and SSE contract.

The load-bearing assertion is that `rows` always precedes the first `token`.
That ordering is the wire-level form of constitution Principle III: no prose can
be produced before the data it describes exists. A stream that emits tokens
first is a contract violation regardless of whether the prose happens to be
right.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import graph as agent_graph
from app.main import create_app


def _events(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into (event, payload) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    event = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event:
            out.append((event, json.loads(line.split(":", 1)[1].strip())))
    return out


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """App with the agent graph replaced by a scripted run."""

    class ScriptedGraph:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        async def astream(self, state: Any, config: Any, stream_mode: str) -> Any:
            for update in self.updates:
                yield update

    scripted = ScriptedGraph()
    monkeypatch.setattr(agent_graph, "get_graph", lambda: scripted)

    app = create_app()
    with TestClient(app) as test_client:
        test_client.scripted = scripted  # type: ignore[attr-defined]
        yield test_client


def _successful_run() -> list[dict[str, Any]]:
    return [
        {"understand": {"resolved_question": "orders in Q3 2014?", "intent": "data"}},
        {"ground": {"entity_matches": {"resolved": {}}}},
        {
            "generate": {
                "pipeline": [{"$match": {"creation_quarter_label": "2014-Q3"}}],
                "explanation": "Counts distinct orders.",
                "expected_shape": "scalar",
            }
        },
        {
            "validate": {
                "pipeline": [
                    {"$match": {"creation_quarter_label": "2014-Q3"}},
                    {"$count": "orders"},
                ],
                "validation_errors": [],
            }
        },
        {
            "execute": {
                "rows": [{"orders": 18_352}],
                "row_count": 1,
                "truncated": False,
                "elapsed_ms": 2_675,
            }
        },
        {"synthesize": {"answer": "18,352 orders were created in Q3 2014.", "chart_spec": None}},
    ]


class TestChatStreamContract:
    def test_event_sequence_matches_the_contract(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        response = client.post("/api/chat", json={"question": "orders in Q3 2014?"})
        names = [name for name, _ in _events(response)]

        assert response.status_code == 200
        assert names[0] == "status"
        assert "pipeline" in names
        assert "rows" in names
        assert "token" in names
        assert names[-1] == "done"

    def test_rows_always_precede_the_first_token(self, client: Any) -> None:
        """Principle III, on the wire."""
        client.scripted.updates = _successful_run()

        names = [name for name, _ in _events(client.post("/api/chat", json={"question": "q"}))]

        assert names.index("rows") < names.index("token")

    def test_pipeline_is_emitted_before_execution(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        names = [name for name, _ in _events(client.post("/api/chat", json={"question": "q"}))]

        assert names.index("pipeline") < names.index("rows")

    def test_rows_event_carries_count_and_timing(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        events = dict(_events(client.post("/api/chat", json={"question": "q"})))

        assert events["rows"]["row_count"] == 1
        assert events["rows"]["elapsed_ms"] == 2_675
        assert events["rows"]["truncated"] is False

    def test_done_carries_session_and_answer(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        events = dict(_events(client.post("/api/chat", json={"question": "q"})))

        assert events["done"]["session_id"]
        assert "18,352" in events["done"]["answer"]
        assert events["done"]["total_ms"] >= 0

    def test_rejected_pipeline_is_never_emitted(self, client: Any) -> None:
        client.scripted.updates = [
            {"generate": {"pipeline": [{"$out": "stolen"}]}},
            {"validate": {"validation_errors": ["stage $out is not permitted"]}},
            {"give_up": {"answer": "I couldn't build a valid query."}},
        ]

        events = _events(client.post("/api/chat", json={"question": "q"}))
        names = [name for name, _ in events]

        assert "pipeline" not in names
        assert "rows" not in names
        assert names[-1] == "done"

    def test_clarification_replaces_the_answer(self, client: Any) -> None:
        client.scripted.updates = [
            {
                "ground": {
                    "intent": "ambiguous",
                    "clarification": {
                        "question": "Which did you mean?",
                        "candidates": [{"field": "department_name", "values": ["A", "B"]}],
                    },
                }
            },
            {"ask_clarify": {"answer": "Which did you mean?"}},
        ]

        events = dict(_events(client.post("/api/chat", json={"question": "q"})))

        assert "rows" not in events
        assert events["clarification"]["candidates"][0]["values"] == ["A", "B"]


class TestWireFormat:
    """Pins the exact bytes, because the browser client parses them by hand.

    A client that split frames on "\n\n" alone parsed zero events against this
    stream and rendered nothing, while every line-oriented test still passed.
    """

    def test_frames_are_separated_by_crlf_crlf(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        body = client.post("/api/chat", json={"question": "q"}).text

        assert "\r\n\r\n" in body

    def test_every_frame_carries_an_event_and_a_data_line(self, client: Any) -> None:
        client.scripted.updates = _successful_run()

        body = client.post("/api/chat", json={"question": "q"}).text

        for frame in [f for f in body.split("\r\n\r\n") if f.strip()]:
            lines = frame.split("\r\n")
            assert any(line.startswith("event:") for line in lines), frame
            assert any(line.startswith("data:") for line in lines), frame


class TestChatValidation:
    def test_empty_question_is_rejected(self, client: Any) -> None:
        assert client.post("/api/chat", json={"question": ""}).status_code == 422

    def test_missing_question_is_rejected(self, client: Any) -> None:
        assert client.post("/api/chat", json={}).status_code == 422


class TestSessions:
    def test_create_returns_an_id(self, client: Any) -> None:
        response = client.post("/api/sessions")

        assert response.status_code == 201
        assert response.json()["session_id"]

    def test_unknown_session_is_404(self, client: Any) -> None:
        assert client.get("/api/sessions/nope/messages").status_code == 404

    def test_history_carries_the_derivation_record(self, client: Any) -> None:
        """FR-021: every assistant message keeps how it was derived."""
        client.scripted.updates = _successful_run()
        done = dict(_events(client.post("/api/chat", json={"question": "q"})))["done"]

        messages = client.get(f"/api/sessions/{done['session_id']}/messages").json()["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]

        assert assistant[0]["derivation"]["row_count"] == 1
        assert assistant[0]["derivation"]["pipeline"]
        assert assistant[0]["derivation"]["elapsed_ms"] == 2_675


class TestSchemaAndSuggestions:
    def test_schema_reports_the_grain(self, client: Any) -> None:
        body = client.get("/api/schema").json()

        assert body["grain"] == "line item"
        assert body["document_count"] == 346_018
        assert body["distinct_orders"] == 200_533

    def test_schema_warns_about_counting_documents(self, client: Any) -> None:
        assert "purchase_order_number" in client.get("/api/schema").json()["grain_warning"]

    def test_suggestions_cover_the_named_question_types(self, client: Any) -> None:
        texts = " ".join(
            s["text"].lower() for s in client.get("/api/suggestions").json()["suggestions"]
        )

        assert "orders" in texts
        assert "quarter" in texts
        assert "item" in texts
