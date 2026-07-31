# Frontend

Angular 22, standalone components, signals for all state — no NgModules, no
`@Input()`/`@Output()` decorators (uses the `input()` function instead), no
`ChangeDetectorRef`. Source: `frontend/src/app/`.

## Structure

```
core/           ChatService (the streaming client), TypeScript models
chat/           chat-shell — the whole conversation UI, one component
insight/        result-table, result-chart, pipeline-panel — the transparency pieces
shared/         format.pipes.ts — currency/number formatting, shared across insight/
```

There's no routing (`provideRouter` is absent from `app.config.ts`) — this is
a single-view app, so a router would be pure overhead.

## `ChatService` (`core/chat.service.ts`)

The one place that talks to the backend. Three public signals drive the
entire UI:

```typescript
readonly messages = signal<ChatMessage[]>([]);
readonly busy = signal(false);
readonly suggestions = signal<Suggestion[]>([]);
```

`ask(question)` appends a user `ChatMessage`, then an empty pending assistant
`ChatMessage`, then streams the response into that same message object by id
as events arrive — so the template only ever renders `messages()`, and every
SSE event is really just "patch the message with this id."

### Why `fetch` + `ReadableStream`, not `EventSource`

`EventSource` can only issue a `GET`. The chat endpoint is a `POST` — the
question has to go in a request body, not a query string — so it's
unusable here. `ask()` uses:

```typescript
const response = await fetch('/api/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ question, session_id: this.sessionId }),
});
await this.consume(response.body, replyId);
```

### Parsing the stream (`consume`)

Reads `response.body` as a `ReadableStream<Uint8Array>`, decodes to text
incrementally, and buffers until a complete frame is available:

```typescript
const frames = buffer.split(/\r?\n\r?\n/);
buffer = frames.pop() ?? '';
for (const frame of frames) {
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
  }
  this.handle(event, JSON.parse(dataLines.join('\n')), replyId);
}
```

**The regex here is load-bearing, not stylistic.** `sse-starlette` (the
backend's SSE library) separates frames with `\r\n\r\n`. An earlier version
of this parser split on a bare `\n\n`, which never matched a single frame —
the buffer grew without bound and **zero events were ever parsed**, even
though every request completed successfully on the server and the backend
logs showed correct answers being generated. See
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md#the-frontend-parsed-zero-sse-events)
for the full incident, including the before/after event counts measured
against the real byte stream. If you ever touch this parsing logic, verify it
against actual server output, not just a hand-constructed test string — the
bug hid behind every line-oriented tool (curl, the Python test suite) because
those tolerate `\r\n` naturally.

### Event handling (`handle`)

One `switch` per SSE event name, each patching the in-flight message:

| Event | Patch |
|---|---|
| `status` | `phase` — drives the "Reading the question…" / "Writing the query…" live indicator |
| `pipeline` | `derivation.pipeline` (row count / timing arrive later, on `rows`) |
| `rows` | `rows`, and fills in `derivation.rowCount` / `truncated` / `elapsedMs` |
| `token` | Appends `data.text` to `content` — this is what makes the answer appear to type itself |
| `chart` | `chart` — only set when the backend decided the shape warrants one |
| `clarification` | `clarification` (candidates) — the composer then renders these as clickable options instead of/alongside the answer |
| `done` | Captures `session_id` for the *next* call, finalizes `content`, clears `pending` |
| `error` | Clears `pending`, sets `error` and a fallback `content` |

`sessionId` is private instance state on the service, not a signal — the
rest of the app never needs to react to it changing, only `ask()` needs to
read it on the next call.

## Models (`core/models.ts`)

TypeScript mirrors of the backend's SSE/REST payloads (kept in sync by hand
against `backend/app/models/schemas.py` and
[`specs/.../contracts/openapi.yaml`](../specs/001-procurement-chat-assistant/contracts/openapi.yaml) —
there's no codegen step). The one client-only type is `ChatMessage`, which
layers UI state (`pending`, `phase`) over the wire fields — this is the
per-bubble view model the template renders directly.

## Components

### `chat/chat-shell` — the whole conversation

One component, `ChatShellComponent`, injects `ChatService` and exposes its
signals directly to the template (`readonly messages = this.chat.messages`).
`ngOnInit` fires `loadSuggestions()` once.

The template (`chat-shell.html`) is a single loop over `messages()`. Per
message it conditionally renders, in order: a live phase indicator (while
`pending && !content`), the streamed text, clarification option buttons (if
any), a chart (if `chart && rows.length`), a results table (if `rows.length`)
**or** an explicit "The query ran and matched no records" note (if a
pipeline ran but returned nothing and the message isn't still pending), and
the derivation panel (if a pipeline exists at all).

That empty-vs-no-pipeline distinction matters: a message with no `rows` and
no `derivation.pipeline` at all (e.g. a decline, a schema answer, a
clarification) shows neither the table nor the "no records" note — only a
message that actually ran a query and got zero rows back shows that specific
line, so it never gets confused with "this kind of question doesn't run a
query."

### `insight/result-table` — supporting rows (FR-010)

Renders whatever columns the actual result rows have — it doesn't know the
schema, it just does `Object.keys(rows[0])`. Column headers are prettified
(`_` → space, title case; `_id` specifically becomes "Group", since that's
what a bare `$group` accumulator key means to a reader). Capped at 50
visible rows (`limit`) with a "Showing 50 of N rows" note — a separate,
tighter cap than the backend's 200-row `MAX_RESULT_ROWS`, because 200 rows in
an HTML table is not a good reading experience even when it's a fine payload
size.

Cell formatting goes through `shared/format.pipes.ts::CellValuePipe`
(`null`/`undefined` → em dash, booleans → Yes/No, integers get thousands
separators, non-integers get exactly 2 decimal places) — deliberately **not
rounded further**, because the answer prose quotes exact figures and a table
that visibly disagreed with the text beside it would undermine the entire
point of showing both.

### `insight/result-chart` — bar/line charts (FR-022)

Thin wrapper around `ng2-charts`' `BaseChartDirective`. Takes the backend's
`ChartSpec` (`type`, `x_field`, `y_field`, `title`) and the same `rows` the
table renders, and builds a Chart.js `data`/`options` pair from them.

Capped at the **first 15** rows (a chart with 90 bars is unreadable; the
table beneath it carries the rest). Axis and tooltip values go through a
`compact()` formatter (`$28.8B`, `$1.0M`, `$4.5K`) because procurement totals
run into the billions and full digits make the axis illegible.

Chart.js itself is registered once, globally, via `provideCharts(withDefaultRegisterables())`
in `app.config.ts` — required by `ng2-charts` v10; omitting it renders an
empty canvas with no error.

### `insight/pipeline-panel` — the transparency panel (constitution Principle VI)

A collapsible `<pre>` block: pipeline JSON, row count, elapsed ms, and (when
`attempts > 1`) how many repair attempts it took, plus a "truncated" note
when applicable. Closed by default (`signal(false)`), so it doesn't compete
with the answer for attention but is one click away — this is what a
reviewer opens to check "did the agent actually query what it claims to
have queried."

## Dev proxy

`frontend/proxy.conf.json` forwards `/api` and `/health` to
`http://localhost:8000`, wired into `angular.json`'s `serve.options`. This is
what lets the Angular dev server (port 4200) and the FastAPI backend (port
8000) be addressed as one origin from the browser's point of view — see
[`docs/operations.md`](operations.md#running-it-locally) for the two-terminal
setup this requires.

## Building

```bash
cd frontend
npm install
npx ng serve            # dev server with the proxy, hot reload
npx ng build            # production bundle → dist/frontend
```
