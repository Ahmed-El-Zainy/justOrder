# Operations

Running it, reading its signals, and diagnosing the failure modes actually
encountered while building it.

## Running it locally

Two servers, two terminals. This is the most common point of confusion —
`GET /` on port 8000 will 404, because the API doesn't serve HTML at all;
the UI lives on a separate Angular dev server.

```bash
# terminal 1 — MongoDB (only the database needs a container for local dev)
docker compose up -d mongo     # or: docker-compose up -d mongo, see below

# terminal 2 — backend
cd backend
uv run uvicorn app.main:app --port 8000

# terminal 3 — frontend
cd frontend
npx ng serve
```

Then open **http://localhost:4200** — not 8000. The Angular dev server
proxies `/api` and `/health` to `localhost:8000` (`frontend/proxy.conf.json`),
so from the browser's perspective it's all one origin.

First-time setup, before any of the above:

```bash
cp .env.example .env    # fill in OPENROUTER_API_KEY
uv run python -m data_pipeline.download
uv run python -m data_pipeline.profile
uv run python -m data_pipeline.load
uv run python -m data_pipeline.load --verify
```

### `docker compose` vs. `docker-compose`

If `docker compose up` (the plugin form) says `unknown command: docker
compose`, the CLI plugin symlink may be dangling (pointing at an uninstalled
Docker Desktop). The standalone `docker-compose` binary (installed separately,
e.g. via Homebrew) works identically for this project — use whichever
resolves on your machine; nothing here is written assuming one specific
invocation.

### Colima

If you're running Docker via [Colima](https://github.com/abiosoft/colima)
rather than Docker Desktop, `colima start` must be running before either
`docker` or `docker-compose` will connect. A machine restart or sleep can
stop it silently — `colima status` / `colima start` first if any `docker`
command reports it can't reach the daemon. **The MongoDB data survives** in
the named `mongo_data` volume across a Colima restart; only the running
container needs to be brought back up (`docker-compose up -d mongo`), not the
data reloaded.

## Reading `/health`

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "mongo": {"connected": true, "document_count": 346018},
  "llm": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "reachable": true}
}
```

`status` is `"degraded"` (HTTP `503`) whenever **any** of the following is
true — the response tells you which:

| Symptom in the payload | Meaning | Fix |
|---|---|---|
| `mongo.connected: false` | Can't reach MongoDB at all | Is the container running? `docker ps`. Is Colima up? |
| `mongo.connected: true`, `document_count: 0` | Mongo is fine, but the collection is empty | `uv run python -m data_pipeline.load` was never run |
| `llm.reachable: false` | The model provider call failed | See "Diagnosing a stuck or failed question" below |

The `document_count: 0` case is deliberately distinguished from a Mongo
connection failure — without it, "containers up but data never loaded"
presents as a confusing run of empty answers with no obvious cause.

### Healthcheck billing

`llm.reachable` is a real model call, but it's cached for 60 seconds
(`app/agent/llm.py::reachable`, `_PROBE_TTL_S`). The Docker healthcheck in
`docker-compose.yml` polls `/health` every 15 seconds; without the cache that
would bill four model calls a minute, forever, just to learn something that
changes rarely.

## Configuration reference

All settings are read once, at process start, from environment variables via
`app/config.py::Settings` (pydantic-settings), which resolves `.env` from an
**absolute path** (`REPO_ROOT / ".env"`) rather than the working directory —
because uvicorn is started from `backend/` and the data pipeline from the
repo root, and a relative `./.env` would silently resolve to nothing in one
of the two (see
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md#env-resolved-relative-to-the-working-directory)).

| Variable | Default | Effect |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | The only value you must supply yourself |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint works |
| `LLM_MODEL` | `anthropic/claude-sonnet-5` | Read fresh on every call, never hardcoded — see [`docs/evaluation.md`](evaluation.md) for how this makes model comparison a one-line change |
| `LLM_TEMPERATURE` | `0.0` | |
| `MONGO_URI` | `mongodb://procurement_app:...@localhost:27017/...` | The **read-only** application credential |
| `MONGO_ADMIN_URI` | `mongodb://root:...@localhost:27017/...` | Used only by `data_pipeline/load.py`, never by the running server |
| `MAX_RESULT_ROWS` | `200` | FR-013 — hard cap on rows returned per question |
| `MAX_REPAIR_ATTEMPTS` | `3` | FR-008 — repair loop ceiling, see [`docs/agent.md`](agent.md#node-by-node), `repair` section |
| `QUESTION_DEADLINE_S` | **`60`** | See [below](#the-per-question-deadline) — SC-004 specifies 30 |
| `QUERY_TIMEOUT_MS` | `15000` | FR-026 — passed to MongoDB as `maxTimeMS` |
| `FUZZY_MATCH_THRESHOLD` | `90` | Entity grounding: score at/above this substitutes silently |
| `FUZZY_AMBIGUOUS_FLOOR` | `75` | Below this, no match at all; between floor and threshold, ask |
| `DATA_COVERAGE_START` / `_END` | `2012-07-01` / `2015-06-30` | Drives FR-015 ("that period isn't covered") |
| `CORS_ORIGINS` | `http://localhost:4200` | Comma-separated if more than one |
| `LOG_LEVEL` | `INFO` | structlog JSON output |

`docker-compose.yml` sets its own inline defaults (e.g.
`QUESTION_DEADLINE_S:-30`) as a fallback *only if no `.env` file exists at
all* — Compose auto-loads a `.env` file from the project directory for
`${VAR}` substitution, so in practice your `.env` values win when both
`docker-compose up` and a local `uv run uvicorn` are used against the same
checkout.

## The per-question deadline

**Ships at 60 seconds. The spec (SC-004) asks for 30.** This is a deliberate,
documented deviation, not an oversight — set `QUESTION_DEADLINE_S=30` to
reproduce the spec value exactly (e.g. for re-running the model comparison in
[`docs/evaluation.md`](evaluation.md) under the original constraint).

### Why it was raised

Every answer logs a per-node timing breakdown:

```json
{"event": "chat.answered", "total_ms": 12314, "mongo_ms": 109,
 "node_ms": {"understand": 6212, "generate": 3311, "synthesize": 2680}}
```

`mongo_ms` — the actual database query — was **0.06–0.4 seconds** across every
measurement taken. **The database was never the bottleneck.** The cost is
three sequential model calls per question (`understand`, `generate`,
`synthesize`), and the provider's latency is highly variable: three
*identical* `generate` requests, back to back, measured 3.4s, 20.7s, and
6.3s. No amount of prompt tuning survives that kind of swing.

At the spec's 30-second value, this cost `deepseek/deepseek-v4-flash` four
otherwise-correct answers in the published comparison — every one of its
failures was the clock, not a wrong answer (see
[`docs/evaluation.md`](evaluation.md#published-results)). Raising the ceiling
was the fix; tuning the prompt would not have been, since the bottleneck is
provider latency variance, not prompt size (measured at ~2,850 tokens for the
generator's system prompt — not large).

### If you see a `deadline_exceeded` error

That's the assistant correctly giving up rather than hanging — see the
`node_ms` breakdown in the log line for that request to see which of the
three model calls was slow. A single very slow `generate` call is the most
common cause; a narrower, more specific question sometimes helps because it
gives the model less to reason about, but the honest fix is that the
provider itself was slow for that call.

## Diagnosing a stuck or failed question

In order of what to check:

1. **`/health`** — is the LLM actually reachable right now? A `402` (no
   credits) or `429` (rate limited) will show as `llm.reachable: false`.
2. **Backend log**, filtered to the relevant event names:
   ```bash
   grep -E "understand|generate_pipeline|execute|chat\.(answered|failed|deadline_exceeded)|llm\.unavailable" backend.log
   ```
   `llm.unavailable` lines carry the raw provider error message (rate limit
   text, credit balance, etc.) — this is almost always the fastest way to
   find out *why* a question failed.
3. **The frontend's pipeline panel** (click "How this was answered" on any
   answered message) — shows the exact pipeline that ran, row count, and
   elapsed ms, without needing to look at logs at all.
4. **Browser DevTools → Network → the `/api/chat` request → "EventStream"
   tab** — shows each SSE event as it arrives with timestamps. If events are
   listed there but nothing rendered in the UI, that's a client-side parsing
   bug, not a backend problem — see
   [`docs/decisions-and-bugs.md`](decisions-and-bugs.md#the-frontend-parsed-zero-sse-events)
   for exactly this failure mode and how it was found.

### Common causes, by symptom

| Symptom | Likely cause | Where to look |
|---|---|---|
| Answers never appear, but the backend log shows `chat.answered` with a real `total_ms` | Client-side SSE parsing is broken | [`docs/decisions-and-bugs.md`](decisions-and-bugs.md#the-frontend-parsed-zero-sse-events) |
| Every question errors immediately with `llm_unavailable` | Provider outage: rate limit or no credits | `/health`, or the raw error in the `llm.unavailable` log line |
| Answers are slow (20–60s) but eventually arrive | Normal — three sequential model calls, provider latency varies | [The per-question deadline](#the-per-question-deadline) above |
| A question that should work instead gets `deadline_exceeded` | The deadline fired mid-call | Check `node_ms` in the log line for that request |
| `GET /` on port 8000 returns 404 | You're pointed at the API, not the UI | Use `localhost:4200`, not `8000` |
| A specific number in an answer looks wrong | Could be a real agent bug | Cross-check against `evals/ground_truth.json` or re-run `evals/run_eval.py --category <name>` for that question type |

## Running the eval harness

```bash
uv run python -m evals.ground_truth        # (re)compute expected answers
uv run python -m evals.run_eval --status   # see what's recorded
uv run python -m evals.run_eval            # run everything outstanding
```

Full detail on the batching, the model comparison, and published results:
[`docs/evaluation.md`](evaluation.md).

## Running the test suite

```bash
cd backend
uv run pytest tests -v          # 185 tests
uv run ruff check app tests     # lint
uv run ruff format app tests    # format
```

`tests/test_guards.py` and `tests/test_api.py::TestWireFormat` are the two
suites most worth understanding before touching the validator or the SSE
framing respectively — see [`docs/agent.md`](agent.md#the-validator) and
[`docs/api.md`](api.md) for what they pin down and why.
