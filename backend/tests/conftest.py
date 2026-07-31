"""Shared fixtures.

Everything here stubs the model and the database, so the suite runs offline and
costs nothing. Behaviour that genuinely needs a live model is verified in the
evaluation harness, not in unit tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import llm, vocabulary


class StubModel:
    """Scripted model responses, so node behaviour is deterministic."""

    def __init__(self) -> None:
        self.json_responses: list[Any] = []
        self.text_responses: list[str] = []
        self.json_calls: list[tuple[str, str]] = []
        self.text_calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str, **_: Any) -> dict[str, Any]:
        self.json_calls.append((system, user))
        if not self.json_responses:
            raise llm.LLMUnavailable("stub exhausted")
        response = self.json_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(self, system: str, user: str, **_: Any) -> str:
        self.text_calls.append((system, user))
        if not self.text_responses:
            return "Stub answer."
        return self.text_responses.pop(0)


@pytest.fixture
def stub_model(monkeypatch: pytest.MonkeyPatch) -> StubModel:
    stub = StubModel()
    monkeypatch.setattr(llm, "complete_json", stub.complete_json)
    monkeypatch.setattr(llm, "complete", stub.complete)
    return stub


@pytest.fixture
def loaded_vocabulary() -> None:
    """Real-shaped values, so grounding behaves as it does against the dataset."""
    vocabulary.set_vocabulary(
        "department_name",
        {
            "Consumer Affairs, Department of": 5_000,
            "Corrections and Rehabilitation, Department of": 40_000,
            "Board of State and Community Corrections": 900,
            "Transportation, Department of": 30_000,
        },
    )
    vocabulary.set_vocabulary(
        "acquisition_type",
        {
            "NON-IT Goods": 215_083,
            "NON-IT Services": 68_372,
            "IT Goods": 50_900,
            "IT Services": 11_516,
            "IT Telecommunications": 147,
        },
    )


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self) -> FakeCursor:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeCollection:
    """Records the pipeline it was given and returns scripted rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.pipelines: list[list[dict[str, Any]]] = []
        self.error: Exception | None = None

    async def aggregate(self, pipeline: list[dict[str, Any]], **_: Any) -> FakeCursor:
        self.pipelines.append(pipeline)
        if self.error is not None:
            raise self.error
        return FakeCursor(self.rows)


@pytest.fixture
def fake_collection(monkeypatch: pytest.MonkeyPatch) -> FakeCollection:
    collection = FakeCollection()

    from app.agent.nodes import execute as execute_node

    monkeypatch.setattr(execute_node, "get_collection", lambda: collection, raising=False)

    import app.db.client as db_client

    monkeypatch.setattr(db_client, "get_collection", lambda: collection)
    return collection
