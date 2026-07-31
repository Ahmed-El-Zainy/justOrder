"""Request, response, and SSE event schemas.

Mirrors specs/001-procurement-chat-assistant/contracts/openapi.yaml.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Phase(StrEnum):
    UNDERSTANDING = "understanding"
    GROUNDING = "grounding"
    GENERATING = "generating"
    VALIDATING = "validating"
    EXECUTING = "executing"
    SYNTHESIZING = "synthesizing"


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "validation_failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    QUERY_TIMEOUT = "query_timeout"
    LLM_UNAVAILABLE = "llm_unavailable"
    INTERNAL_ERROR = "internal_error"


class ChatRequest(BaseModel):
    session_id: str | None = None
    question: str = Field(min_length=1, max_length=2000)


# --- SSE event payloads -----------------------------------------------------


class StatusEvent(BaseModel):
    phase: Phase
    detail: str = ""


class PipelineEvent(BaseModel):
    pipeline: list[dict[str, Any]]
    collection: str = "purchase_orders"
    explanation: str = ""
    attempt: int = 1


class RowsEvent(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False
    elapsed_ms: int


class TokenEvent(BaseModel):
    text: str


class ChartEvent(BaseModel):
    type: Literal["bar", "line"]
    x_field: str
    y_field: str
    title: str = ""


class ClarificationCandidate(BaseModel):
    field: str
    values: list[str]


class ClarificationEvent(BaseModel):
    question: str
    candidates: list[ClarificationCandidate] = Field(default_factory=list)


class DoneEvent(BaseModel):
    session_id: str
    message_id: str
    answer: str
    total_ms: int


class ErrorEvent(BaseModel):
    code: ErrorCode
    message: str
    recoverable: bool = True


# --- REST payloads ----------------------------------------------------------


class Derivation(BaseModel):
    """How an answer was produced — the record FR-021 requires."""

    pipeline: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    attempts: int = 1


class Message(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    derivation: Derivation | None = None


class MessageList(BaseModel):
    session_id: str
    messages: list[Message]


class SessionCreated(BaseModel):
    session_id: str
    created_at: datetime


class FieldInfo(BaseModel):
    name: str
    type: str
    description: str = ""
    nullable: bool = False


class Coverage(BaseModel):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    fiscal_years: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SchemaCard(BaseModel):
    collection: str
    document_count: int
    distinct_orders: int = 0
    grain: str = "line item"
    coverage: Coverage | None = None
    fields: list[FieldInfo] = Field(default_factory=list)
    vocabularies: dict[str, list[str]] = Field(default_factory=dict)


class Suggestion(BaseModel):
    text: str
    category: str = ""


class SuggestionList(BaseModel):
    suggestions: list[Suggestion]


class MongoHealth(BaseModel):
    connected: bool
    document_count: int


class LLMHealth(BaseModel):
    provider: str
    model: str
    reachable: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    mongo: MongoHealth
    llm: LLMHealth
