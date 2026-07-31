"""SSE event framing.

The `rows` event always precedes the first `token`. That ordering is the
wire-level form of constitution Principle III: no prose can be produced before
the data it describes exists.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def frame(event: str, payload: BaseModel | dict[str, Any]) -> dict[str, str]:
    """One SSE frame, in the shape sse-starlette expects."""
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return {"event": event, "data": json.dumps(data, default=str)}
