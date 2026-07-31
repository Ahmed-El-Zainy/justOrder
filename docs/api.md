# API Reference

Human-readable reference for the backend HTTP/SSE API. The
machine-readable contract (OpenAPI 3.1) lives at
[`specs/001-procurement-chat-assistant/contracts/openapi.yaml`](../specs/001-procurement-chat-assistant/contracts/openapi.yaml);
this document explains the parts OpenAPI can't express well, particularly the
SSE event sequence, and reflects the API as actually implemented in
`backend/app/api/routes.py` and `backend/app/main.py`.

Base URL: `http://localhost:8000`. No authentication — out of scope per the
spec (single-user prototype).

## `GET /health`

Defined on the app itself (`main.py`), not under `/api`.

```json
{
  "status": "ok",
  "mongo": {"connected": true, "document_count": 346018},
  "llm": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "reachable": true}
}
```

`status` is `"ok"` only when **all three** are true: Mongo is connected, the
collection is non-empty, and the LLM probe succeeded. Otherwise `"degraded"`
with HTTP `503`. The middle condition — `document_count > 0` — exists
specifically so "the containers are up but nobody ran the loader" shows up as
`degraded` rather than as a confusing run of empty answers with no clear
cause.

The `llm.reachable` check is a real model call (`"Reply with OK."`,
`max_tokens=5`), cached for 60 seconds (`app/agent/llm.py::reachable()`) so a
15-second container healthcheck doesn't bill the provider four times a
minute — see [`docs/operations.md`](operations.md#healthcheck-billing).

## `POST /api/chat`

The only endpoint that talks to the agent. Request:

```json
{"question": "How many orders were created in Q3 2014?", "session_id": null}
```

`session_id` is optional — omit it (or pass `null`) to start a new session;
the server returns the real id on the `done` event, and the client should
send it back on every subsequent turn to keep conversation context (see
[`docs/agent.md`](agent.md) for how follow-ups are resolved against it).

`question`: 1–2000 characters (`ChatRequest` in `app/models/schemas.py`).
Empty or missing → `422`.

**Response**: `text/event-stream`. `Content-Type` per frame is fixed;
events are separated by a blank line. **The server writes `\r\n\r\n` between
frames** (that's what `sse-starlette` does) — a client that only splits on
`\n\n` will parse nothing at all. See
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md#the-frontend-parsed-zero-sse-events)
for the incident this caused and the exact bytes.

### Event sequence

```
status(understanding)
  → status(grounding)?
  → status(generating) → pipeline → status(validating)?
  → status(executing) → rows
  → status(synthesizing) → token* → chart?
  → done
```

`grounding` and `validating` are emitted opportunistically as the graph
passes through those nodes — they don't always appear (e.g. a `schema` or
`out_of_scope` intent skips straight past grounding/generation entirely).
`clarification` replaces the rest of a normal answer sequence when the agent
needs to ask rather than answer:

```
status(understanding) → status(grounding) → clarification → status(synthesizing) → token* → done
```

Any point in the sequence can instead end in `error`.

### Events

| Event | Payload | When |
|---|---|---|
| `status` | `{"phase": "...", "detail": "..."}` | Once per graph node transition. `phase` is one of `understanding`, `grounding`, `generating`, `validating`, `executing`, `synthesizing` |
| `pipeline` | `{"pipeline": [...], "explanation": "...", "attempt": 1}` | Once validation succeeds, **before** execution. `attempt` increments on a repair |
| `rows` | `{"rows": [...], "row_count": N, "truncated": bool, "elapsed_ms": N}` | After execution. `rows` is capped at 200 (`MAX_RESULT_ROWS`) |
| `token` | `{"text": "..."}` | Repeatedly, as the answer is chunked (≈24-char word-aligned chunks — see `routes.py::_chunks`) |
| `chart` | `{"type": "bar"\|"line", "x_field": "...", "y_field": "...", "title": "..."}` | Only when the result shape is a ranking or time series (FR-022) — never for a scalar |
| `clarification` | `{"question": "...", "candidates": [{"field": "...", "values": [...]}]}` | When a named entity matches several stored values |
| `done` | `{"session_id": "...", "message_id": "...", "answer": "...", "total_ms": N}` | Terminal on success |
| `error` | `{"code": "...", "message": "...", "recoverable": true}` | Terminal on failure |

### `error.code` values

| Code | Meaning | Cause in the agent |
|---|---|---|
| `validation_failed` | No valid pipeline after 3 repair attempts | `give_up` reached via `_after_validate`/`_after_execute` |
| `deadline_exceeded` | The whole request exceeded `QUESTION_DEADLINE_S` | `asyncio.timeout` fired in `routes.py` |
| `query_timeout` | MongoDB's own `maxTimeMS` fired | `execute` node caught `ExecutionTimeout` |
| `llm_unavailable` | The model provider could not be reached (rate limit, no credits, network) | `generate` node caught `LLMUnavailable`; routed straight to `give_up`, skipping repair entirely |
| `internal_error` | Anything else | Caught at the top of `_stream_answer` |

**The grounding guarantee**: the `rows` event always precedes the first
`token` event, in every successful path. This is the wire-level expression
of "no invented figures" — synthesis literally cannot start emitting tokens
before the data it's describing has been sent. Pinned directly by
`backend/tests/test_api.py::TestChatStreamContract::test_rows_always_precede_the_first_token`.

## `POST /api/sessions`

Creates an empty session without asking a question.

```json
{"session_id": "b3f1...", "created_at": "2026-07-28T11:00:00Z"}
```

Not required before `POST /api/chat` — that endpoint creates one
automatically if `session_id` is omitted. Useful if a client wants the id
before the first question is typed.

## `GET /api/sessions/{session_id}/messages`

```json
{
  "session_id": "b3f1...",
  "messages": [
    {"message_id": "...", "role": "user", "content": "...", "created_at": "..."},
    {
      "message_id": "...", "role": "assistant", "content": "...", "created_at": "...",
      "derivation": {
        "pipeline": [...], "row_count": 12, "truncated": false,
        "elapsed_ms": 312, "attempts": 1
      }
    }
  ]
}
```

`404` if the session id is unknown. Every assistant message carries a
`derivation` object — the same information the `pipeline`/`rows` SSE events
carried live, persisted so it can be retrieved after the fact. This is what
FR-021 (transparency) requires and what the frontend's "How this was
answered" panel reads when replaying history.

Sessions are **in-memory only** (`app/api/sessions.py`) — capped at
`MAX_SESSIONS = 200` (oldest evicted first) and `MAX_TURNS_PER_SESSION = 100`
per session. Nothing survives a backend restart; this is a stated
out-of-scope item in the spec, not an oversight.

## `GET /api/schema`

Returns the generated schema card verbatim
(`data_pipeline/schema_card.json`, read live by `agent/prompts/__init__.py::schema_card()`):
document count, distinct order count, the grain warning, coverage dates,
per-field descriptions, and enum vocabularies. `503` if the file doesn't
exist yet (i.e., `data_pipeline.profile` was never run).

This is the same content the generation prompt receives — see
[`docs/agent.md`](agent.md#generate-nodesgeneratepy-function-generate_pipeline) — exposed
so a client can show the user what the assistant actually knows about the
data.

## `GET /api/suggestions`

```json
{"suggestions": [{"text": "How many orders were created in Q3 2014?", "category": "counting"}]}
```

Six fixed starter questions (`routes.py::get_suggestions`), covering the
assessment's named question types. Exists for SC-009 — a first-time user
should be able to ask something useful without instruction.

## Streaming from a client other than the shipped Angular app

Because the response is a POST with an SSE body, `EventSource` cannot be
used (it only issues GET). Any client must:

1. `fetch('/api/chat', {method: 'POST', body: JSON.stringify({question, session_id})})`
2. Read `response.body` as a `ReadableStream`, decode as UTF-8.
3. **Split frames on `/\r?\n\r?\n/`**, not a bare `\n\n` — see the warning
   above.
4. Within a frame, split lines on `/\r?\n/`; a line starting `event:` sets
   the event name, a line starting `data:` is the JSON payload.

This is exactly what `frontend/src/app/core/chat.service.ts` does — see
[`docs/frontend.md`](frontend.md) for the full client implementation, and
`backend/tests/test_api.py::TestWireFormat` for a test that pins the literal
bytes so this contract can't silently regress again.
