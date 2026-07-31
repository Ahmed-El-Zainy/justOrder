"""In-memory conversation store.

Session-scoped only — the spec places cross-restart persistence out of scope.
Each assistant turn keeps its derivation record, which is what FR-021 requires
and what `GET /api/sessions/{id}/messages` returns.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

MAX_SESSIONS = 200
MAX_TURNS_PER_SESSION = 100

_sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()


def create() -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "created_at": datetime.now(UTC),
        "messages": [],
    }
    _sessions[session_id] = session

    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)

    return session


def ensure(session_id: str | None) -> dict[str, Any]:
    if session_id and session_id in _sessions:
        _sessions.move_to_end(session_id)
        return _sessions[session_id]
    return create()


def get(session_id: str) -> dict[str, Any] | None:
    return _sessions.get(session_id)


def add_message(
    session_id: str,
    role: str,
    content: str,
    derivation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = _sessions.get(session_id) or create()
    message = {
        "message_id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "created_at": datetime.now(UTC),
        "derivation": derivation,
    }
    session["messages"].append(message)
    del session["messages"][:-MAX_TURNS_PER_SESSION]
    return message


def history(session_id: str) -> list[dict[str, str]]:
    """Recent turns, in the shape the understand node expects."""
    session = _sessions.get(session_id)
    if not session:
        return []
    return [
        {"role": message["role"], "content": message["content"]} for message in session["messages"]
    ]
