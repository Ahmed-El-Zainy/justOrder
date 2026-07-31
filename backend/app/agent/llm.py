"""Model access through an OpenAI-compatible endpoint.

Pointed at OpenRouter by default, but any OpenAI-compatible base URL works. The
model id comes from configuration, never from a call site — the constitution's
Technology Constraints require that, and it is what makes the evaluation
harness's model-comparison sweep a one-variable change.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog
from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import get_settings

log = structlog.get_logger(__name__)

_client: AsyncOpenAI | None = None

# Models sometimes wrap JSON in a fenced block despite being asked not to.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """The provider could not be reached, or refused the request."""


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            raise LLMUnavailable("OPENROUTER_API_KEY is not set")
        _client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=60.0,
            max_retries=1,
        )
    return _client


async def complete(
    system: str,
    user: str,
    *,
    max_tokens: int = 1024,
    temperature: float | None = None,
) -> str:
    """Plain text completion."""
    settings = get_settings()
    try:
        response = await get_client().chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=settings.llm_temperature if temperature is None else temperature,
        )
    except (RateLimitError, APITimeoutError, APIError) as exc:
        log.warning("llm.unavailable", error=str(exc), model=settings.llm_model)
        raise LLMUnavailable(str(exc)) from exc

    return (response.choices[0].message.content or "").strip()


async def complete_json(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Structured completion, parsed to a dict.

    Requests JSON mode where the provider supports it, and still tolerates a
    fenced response — a parse failure here would otherwise burn a repair
    attempt on a formatting problem rather than a query problem.
    """
    settings = get_settings()
    try:
        response = await get_client().chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=settings.llm_temperature if temperature is None else temperature,
            response_format={"type": "json_object"},
        )
    except (RateLimitError, APITimeoutError, APIError) as exc:
        log.warning("llm.unavailable", error=str(exc), model=settings.llm_model)
        raise LLMUnavailable(str(exc)) from exc

    return parse_json(response.choices[0].message.content or "")


def parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"model did not return valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMUnavailable(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


async def stream(
    system: str,
    user: str,
    *,
    max_tokens: int = 1024,
    temperature: float | None = None,
) -> Any:
    """Token stream for the synthesized answer."""
    settings = get_settings()
    try:
        return await get_client().chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=settings.llm_temperature if temperature is None else temperature,
            stream=True,
        )
    except (RateLimitError, APITimeoutError, APIError) as exc:
        log.warning("llm.unavailable", error=str(exc), model=settings.llm_model)
        raise LLMUnavailable(str(exc)) from exc


_probe_cache: tuple[float, bool] | None = None
_PROBE_TTL_S = 60.0


async def reachable() -> bool:
    """Liveness probe for the health endpoint, cached for a minute.

    The container healthcheck hits /health every 15 seconds. Without the cache
    that would bill a model request four times a minute, forever, to learn
    something that changes rarely.
    """
    global _probe_cache

    now = time.monotonic()
    if _probe_cache is not None and now - _probe_cache[0] < _PROBE_TTL_S:
        return _probe_cache[1]

    try:
        await complete("Reply with OK.", "ping", max_tokens=5)
        ok = True
    except Exception as exc:
        log.warning("llm.probe_failed", error=str(exc))
        ok = False

    _probe_cache = (now, ok)
    return ok
