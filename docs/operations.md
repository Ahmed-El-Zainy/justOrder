# Operations

Running it, reading its signals, and diagnosing the failure modes actually
encountered while building it.

## Running it locally

Two servers, two terminals. This is the most common point of confusion —
`GET /` on port 8000 will 404, because the API doesn't serve HTML at all;
the UI lives on a separate Angular dev server.

### First-time setup (once per checkout)

```bash
cp .env.example .env    # fill in OPENROUTER_API_KEY
uv run python -m data_pipeline.download   # ~163 MB CSV, skipped if present
uv run python -m data_pipeline.profile    # writes the schema card
cd frontend && npm install && cd ..
```

The download and profile steps are genuinely once-only: the CSV lands in
`kaggle_data/` and the schema card in `data_pipeline/schema_card.json`, both
of which persist in your working tree. **The database load is not
once-only** — see the next section for why.

### Every-time startup sequence

Run these in order. The order is load-bearing, and every step has a check
that fails loudly rather than producing a system that looks up but answers
wrongly.

**1. Start the Docker daemon.** With [Colima](https://github.com/abiosoft/colima),
this stops on reboot or sleep and does not restart itself:

```bash
colima status || colima start
```

**2. Bring up MongoDB and wait for healthy.**

```bash
docker-compose up -d mongo      # or: docker compose up -d mongo
docker ps --filter name=procurement-mongo    # want: Up … (healthy)
```

The container is declared `restart: unless-stopped`, so once the daemon is
back it usually restarts *itself* before you get here. In that case
`docker-compose up -d mongo` prints `Conflict. The container name
"/procurement-mongo" is already in use` — **this is benign**: it means the
container is already running. Confirm with `docker ps` and move on. Only if
the container exists but is stopped do you need `docker start
procurement-mongo`.

**3. Verify the database actually has data.** Do not skip this:

```bash
uv run python -m data_pipeline.load --verify
```

Expect `all checks passed`, asserting 346,018 documents, 200,533 distinct
orders, 11 indexes, 6 vocabulary fields, a typed `double` `total_price`, and
1,438 surviving negative rows. If it reports 0 documents, load it:

```bash
uv run python -m data_pipeline.load       # drops and reloads, then verifies
```

The load takes a few minutes and needs the **admin** credential, which it
uses by default (`data_pipeline/load.py`) — it's the only part of the project
that writes.

**4. Start the backend — only after step 3 passes.**

```bash
cd backend && uv run uvicorn app.main:app --port 8000
```

Watch the startup lines. `vocabulary.loaded` must report non-zero:

```json
{"fields": 6, "values": 176, "event": "vocabulary.loaded"}
```

`{"fields": 0, "values": 0}` means the backend started against an empty
database — stop it, fix step 3, and start it again. See
[Why the backend must start last](#why-the-backend-must-start-last).

**5. Start the frontend.**

```bash
cd frontend && npx ng serve
```

**6. Confirm the whole stack before using it.**

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

`document_count` must be `346018`, not `0`. Then open
**http://localhost:4200** — not 8000. The Angular dev server proxies `/api`
and `/health` to `localhost:8000` (`frontend/proxy.conf.json`), so from the
browser's perspective it's all one origin.

### Why the backend must start last

The grounding vocabulary is warmed **once**, in the FastAPI lifespan
(`backend/app/main.py`, calling `vocabulary.load()`), and a failure there is
caught and logged as a warning rather than being fatal — deliberately, so a
transient database problem doesn't prevent the process from starting at all.

The consequence is that the cache never re-reads on its own. A backend
started before the data was loaded holds an empty vocabulary for the entire
life of the process, and **restarting the backend is the only fix** —
loading the data underneath a running server does not repair it.

That failure is quiet and easy to misread, because the database itself
reconnects fine. Queries start returning rows again while entity grounding
stays dead, so "Department of Consumer Affairs" silently fails to resolve to
the stored `"Consumer Affairs, Department of"` and the assistant reports
zero — the exact confidently-wrong answer that grounding exists to prevent
(see [`docs/agent.md`](agent.md#entity-grounding)).

### `docker compose` vs. `docker-compose`

If `docker compose up` (the plugin form) says `unknown command: docker
compose`, the CLI plugin symlink may be dangling (pointing at an uninstalled
Docker Desktop). The standalone `docker-compose` binary (installed separately,
e.g. via Homebrew) works identically for this project — use whichever
resolves on your machine; nothing here is written assuming one specific
invocation.

### Colima, and why the data does not always survive

If you're running Docker via [Colima](https://github.com/abiosoft/colima)
rather than Docker Desktop, the VM must be started before either `docker` or
`docker-compose` will connect. A machine restart or sleep stops it silently,
and the symptom is a connection error naming the socket:

```
failed to connect to the docker API at unix:///Users/…/.colima/default/docker.sock
```

The backend shows the same outage as a Mongo timeout, at startup and on every
query:

```
localhost:27017: [Errno 61] Connect call failed ('127.0.0.1', 27017)
```

`colima status` / `colima start` is the fix in both cases.

**The MongoDB data usually survives in the named `mongo_data` volume, but do
not assume it.** If the volume is removed — `docker-compose down -v`, a
Colima disk reset, or anything that recreates the volume — Compose creates a
fresh empty one and the container comes back up attached to *that*. The
collections still exist, because `scripts/mongo-init.js` recreates them on an
empty data directory, so nothing about the container's state looks wrong:
it's `Up (healthy)`, the app connects, pipelines validate and execute in tens
of milliseconds, and every query returns zero rows.

This is precisely why step 3 above is a required step rather than a
first-time one. `--verify` distinguishes "empty database" from "correct
database" in about a second; the UI does not.

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
| `mongo.connected: false` | Can't reach MongoDB at all | Is the container running? `docker ps`. Is Colima up? `colima status` |
| `mongo.connected: true`, `document_count: 0` | Mongo is fine, but the collection is empty | Either the load was never run, or the volume was recreated empty — `uv run python -m data_pipeline.load` |
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
| `Connect call failed ('127.0.0.1', 27017)` at startup and on every query | The Docker daemon is down, so the Mongo container isn't running | `colima status` / `colima start`, then `docker ps` — [Colima](#colima-and-why-the-data-does-not-always-survive) |
| `docker-compose up -d mongo` says the container name is already in use | Benign — `restart: unless-stopped` already brought it back | `docker ps`; nothing to do if it shows `(healthy)` |
| Every question answers "no results", `execute` logs `rows: 0` in a few ms, pipelines look correct | The database is empty — reachable but never loaded, or the volume was recreated | `uv run python -m data_pipeline.load --verify` — [step 3](#every-time-startup-sequence) |
| Startup logs `{"fields": 0, "values": 0, "event": "vocabulary.loaded"}` | Backend started before the data existed; the cache is warmed once and never re-reads | Restart the backend — [Why the backend must start last](#why-the-backend-must-start-last) |
| Department/supplier names never match, though other questions return rows | Same empty-vocabulary cache as above; the query path recovered, grounding did not | Restart the backend after confirming `--verify` passes |
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
