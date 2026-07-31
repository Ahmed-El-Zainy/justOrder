# Decisions and Bugs

A narrative log, in the order things were actually found. Use this when the
code does something that looks surprising — the reason is almost always here,
usually because a specific wrong answer or a specific silent failure forced
the fix. Design rationale that doesn't trace back to an incident lives in
[`specs/001-procurement-chat-assistant/research.md`](../specs/001-procurement-chat-assistant/research.md)
instead of being repeated here.

## Bugs found by running the system

Every entry below was found by actually running the assistant or its tests
against real data and a real model — not by reading the code. That's
deliberate: several of these would have looked correct on inspection.

### `.env` resolved relative to the working directory

**Symptom**: `/health` reported `llm.reachable: false` even though
`OPENROUTER_API_KEY` was set correctly in `.env` — the key was visibly
present in the file, and reading the file directly showed no formatting
problem.

**Root cause**: `pydantic-settings`' default `env_file=".env"` resolves
relative to the **process's current working directory**, not the project
root. `uvicorn` is started from `backend/`, but `data_pipeline` scripts are
run from the repo root — so the same `Settings` class silently loaded a
different (or nonexistent) file depending on which directory the command was
run from, with no error, just an empty `openrouter_api_key`.

**Fix**: `app/config.py` resolves an absolute path once,
`REPO_ROOT = Path(__file__).resolve().parents[2]`, and passes
`env_file=(REPO_ROOT / ".env", ".env")` — the absolute path is tried first,
so it's correct regardless of the working directory the process was launched
from; the plain `.env` is kept as a fallback for anyone running from the
repo root directly.

**Where**: `backend/app/config.py`.

---

### async `aggregate()` never awaited

**Symptom**: every single question failed at execution, immediately, with
`RuntimeWarning: coroutine 'AsyncCollection.aggregate' was never awaited`.
The generated pipelines were all correct — including the critical `$group`
on `purchase_order_number` for order counting — but no query ever actually
ran.

**Root cause**: PyMongo's `AsyncMongoClient.aggregate()` is itself a
coroutine that resolves to the cursor; it has to be awaited *before*
iterating, not just iterated with `async for`. This is different from Motor's
API, which returns the cursor synchronously.

**Fix**: `cursor = await get_collection().aggregate(pipeline, ...)` in
`app/agent/nodes/execute.py` (and the equivalent call in
`app/agent/vocabulary.py::resolve_on_demand`).

**Where**: `backend/app/agent/nodes/execute.py`.

---

### Entity grounding scoring on filler tokens

**Symptom**: caught while writing `test_vocabulary.py` against the *real*
loaded vocabulary (not a hand-picked fixture) — `"Ministry of Magic"` scored
**85.5** against four unrelated real departments (`"Consumer Affairs,
Department of"`, `"Corrections and Rehabilitation, Department of"`, etc.),
purely because they all share the substring `", Department of"`. A
nonsense query was on the verge of silently resolving to a real department.

**Root cause**: `rapidfuzz.fuzz.WRatio` scored the full strings, and in this
dataset the shared boilerplate (`", Department of"`) dominates the score for
department-style names, drowning out the part that actually distinguishes
one department from another.

**Fix**: matching became a two-pass process
(`app/agent/vocabulary.py::match_value`):
1. Strip filler tokens (`department`, `dept`, `of`, `the`, `and`, `for`, `a`,
   `an`) via `_distinctive()`, then score with `token_set_ratio` — this
   decides who is even a *candidate*. `"Ministry of Magic"` now correctly
   matches nothing.
2. Re-score the survivors with `WRatio` on the **full, unstripped** string —
   because the filler carries real signal once noise is excluded:
   `"Department of Transportation"` should prefer `"Transportation,
   Department of"` over `"Transportation Commission, California"`, and
   stripping filler from *both* would make them look equally close.

A `DECISIVE_MARGIN` (5.0 points) was also added: a candidate must not only
clear the threshold, it must clear it by a margin over the runner-up, or the
match is treated as genuinely ambiguous (asks rather than guesses).

**Where**: `backend/app/agent/vocabulary.py::match_value`, `_distinctive`.
Verified against the real vocabulary, not just fixtures — `"Corrections"`
correctly comes back ambiguous (two real departments collide on it),
`"Department of Transportation"` correctly resolves to one.

---

### Provider outage treated as a repairable query error

**Symptom**: OpenRouter credits ran out mid-session (HTTP 402). The agent
burned all 3 repair attempts in under half a second, then reported *"I
couldn't build a valid query for that question"* — actively misleading,
since the model was never reached at all.

**Root cause**: `generate_pipeline` caught the outage and turned it into a
`validation_errors` entry indistinguishable from "the model produced a bad
pipeline," so it routed into the same repair loop that exists for fixable
mistakes. Retrying a 402 cannot ever succeed.

**Fix**: `generate_pipeline` now tags the failure `error.code =
"llm_unavailable"`, and a new graph edge, `_after_generate`, checks for
exactly that code and routes straight to `give_up` — skipping `validate` and
the repair loop entirely. `give_up` itself now gives a different, honest
message for this case: *"I couldn't reach the language model... a service
problem on my side, not a problem with your question."*

**Where**: `backend/app/agent/graph.py::_after_generate`,
`backend/app/agent/nodes/respond.py::give_up`.

---

### The validator rejecting its own `$group` output as unknown fields

**Symptom**: a genuinely correct follow-up sequence broke. After
`{"$group": {"_id": "$department_name", "spend": {"$sum": "$total_price"}}}`,
a subsequent `{"$sort": {"spend": -1}}` was rejected: *"unknown field(s):
['spend']"*.

**Root cause**: the field-reference check validated every stage against the
*original collection schema*. But `spend` isn't a document field — it's a
name the `$group` stage itself invented. After a `$group`, the only fields
that exist downstream are `_id` and whatever accumulator names that stage
defined. A schema-only check rejects the completely ordinary
`$group → $sort → $project` shape.

**Fix**: `_stage_outputs()` tracks what each stage actually makes available
to the *next* stage — `$group` narrows to `{_id} ∪ accumulator names`,
`$count` narrows to just its output name, `$project`/`$addFields`/`$set`
extend or narrow appropriately, `$replaceRoot` gives up tracking entirely
(the shape becomes whatever the expression produced). `_validate_field_references`
threads this running "available fields" set through the whole pipeline
instead of checking every stage against the same fixed set.

**Where**: `backend/app/agent/guards.py::_stage_outputs`,
`_validate_field_references`. Five dedicated tests added, including the
literal group-sort-project shape that broke.

---

### Follow-ups losing the calendar frame

**Symptom**: found first in the same multi-turn debugging session as the
history bug below, and easy to mistake for the same root cause — it isn't.
Turn 1, *"What was total spending in Q1 2014?"*, worked. Turn 2, *"What
about Q2?"*, generated
`{"$group": {"_id": "$fiscal_quarter_label"}}, {"$match": {"_id": {"$regex":
"Q2$"}}}` — a *fiscal* quarter, matched by regex, **across every year in the
dataset**, when the obviously intended question was "the same measure, same
year, next calendar quarter."

**Root cause**: `UNDERSTAND_SYSTEM` said a follow-up should be "rewritten to
stand alone" but gave no worked example of what that means for a numeric
follow-up like "what about Q2?" — the model had to guess how much context to
carry forward and guessed wrong (fiscal instead of calendar; no year at all).

**Fix**: added four worked examples directly to the prompt showing exactly
which parts of a previous question carry forward by default (measure, year,
calendar basis, category filter) versus what a genuine topic change drops —
including the specific case that broke: *"Previous: 'What was total spending
in Q1 2014?' New: 'What about Q2?' → 'What was total spending in Q2 2014?'"*

**Where**: `backend/app/agent/prompts/__init__.py::UNDERSTAND_SYSTEM`.

---

### History rendered empty — LangChain message objects, not dicts

**Symptom**: multi-turn follow-ups stopped resolving. `"What about Q2?"`
after a Q1 question was classified `ambiguous` instead of being rewritten to
`"What was total spending in Q2 2014?"` — the log showed
`resolved='Break down what by department?'`, i.e. the model saw no prior
context at all.

**Root cause**: `_history()` filtered incoming turns with `isinstance(turn,
dict)`. But the field it was reading (`messages`) carries LangGraph's
`add_messages` reducer, which converts plain dicts into LangChain message
objects — so the filter silently dropped every turn, and every follow-up
looked like a standalone first question to the model.

**Fix, in two parts**:
1. `_render_turn()` now handles both shapes — a plain dict, or an object
   with `.type`/`.content` attributes (mapping LangChain's `"human"`/`"ai"`
   to `"user"`/`"assistant"`).
2. A **separate, non-reduced state field** was added: `history: list[dict]`,
   populated once per call directly from the session store
   (`sessions.history(session_id)[:-1]`). This avoids a second, subtler bug:
   re-feeding the `add_messages`-reduced `messages` field back in as input
   each turn would have **duplicated** every prior turn, since that reducer
   only ever appends.

**Where**: `backend/app/agent/state.py` (the `history` field, with a comment
explaining exactly this), `backend/app/agent/nodes/understand.py::_render_turn`,
`_history`. `backend/tests/test_followups.py::TestHistoryRendering` pins both
shapes explicitly, including a `FakeMessage` fixture shaped like the
LangChain object that broke this.

---

### The synthesizer inventing a total from a partial row slice

**Symptom**: the most serious finding in the whole project. Asked *"What was
total spending by department in Q2 2014?"* (90 departments matched, only the
first 60 shown to the model per `MAX_ROWS_IN_PROMPT`), the answer was:
*"Total spending across all departments in Q2 2014 was $15,110,000,000
(rounded), based on the 60 rows returned... truncated from the original 90
rows."* That figure appears in **no row** — the model summed the 60 it could
see and presented the partial sum as if it were the whole answer.

**Root cause**: the synthesis prompt disclosed truncation (*"you are being
shown only the first 60 of 90 rows"*) but never explicitly forbade
arithmetic. A model handed 60 numbers will, by default, add them up if the
question asks for a total — exactly the confident-but-wrong figure
constitution Principle III exists to prevent.

**Fix, in two parts**:
1. The elision note was made explicit and directive: *"Do not describe the
   rows you cannot see, and do not combine these into a total."*
2. `SYNTHESIZER_SYSTEM` itself was rewritten to forbid calculation outright:
   *"You must not calculate. Do not sum, average, subtract, or round the
   values to produce a figure that is not already present in a row."* Every
   figure in the answer must be **copied** from a row, never derived.

After the fix, the same question correctly answers: *"the total spending
across all departments is not included in the result. The highest spending
department shown is Health Care Services, Department of with
$11,506,448,717.16"* — an exact, verifiable row value, and an explicit
statement that no total is available.

**Where**: `backend/app/agent/nodes/respond.py::synthesize`,
`backend/app/agent/prompts/__init__.py::SYNTHESIZER_SYSTEM`.
`backend/tests/test_followups.py::TestSynthesisDoesNotInventTotals` pins the
elision-disclosure text and the "must not calculate" language directly.

---

### `Intent` `is`-comparison silently failing after a checkpoint round-trip

**Symptom**: found while writing the SSE contract test for the
clarification path, not in production — but would have been a real,
hard-to-reproduce bug: `Intent` is a `StrEnum`, and state that has passed
through LangGraph's `MemorySaver` checkpointer can come back as a plain
Python string rather than the enum instance. `final.get("intent") is
Intent.AMBIGUOUS` is `False` for the string `"ambiguous"`, even though `==`
would be `True`.

**Fix**: changed to `==` in `routes.py`'s clarification-routing check, with
a comment explaining why `is` is the wrong operator here specifically.

**Where**: `backend/app/api/routes.py`.

---

### "IT services" grounded to `item_name` instead of `acquisition_type`

**Symptom**: found by the evaluation harness, not by inspection —
`top_dept_it_services` failed with the wrong department entirely. The log
showed `entities: {"item_name": "IT services"}` — the extraction had
classified an acquisition category as if it were a physical item someone
ordered.

**Root cause**: nothing in the `understand` prompt told the model that
`"IT services"`/`"IT goods"`/`"non-IT goods"` etc. are always
`acquisition_type` values, never item names. The distinction is not obvious
from the phrase alone.

**Fix**: `UNDERSTAND_SYSTEM` now states explicitly: *"'IT services', 'IT
goods', 'IT telecommunications', 'non-IT goods' and 'non-IT services' are
ALWAYS acquisition_type — they are purchase categories, never item names."*
The same convention was also added to the generator's schema card, since
both prompts independently reason about field selection.

**Where**: `backend/app/agent/prompts/__init__.py::UNDERSTAND_SYSTEM`,
`data_pipeline/profile.py`'s `conventions` list (regenerates
`schema_card.json`).

---

### `calcard` matched as the string `"YES"` instead of a boolean

**Symptom**: found by the evaluation harness — *"How many orders were
placed using a CalCard?"* returned *"No matching records were found"*,
even though 3,775 such orders exist in the ground truth.

**Root cause**: `calcard` is stored as a BSON boolean (`true`/`false`), but
nothing in the schema card said so explicitly enough — the model generated
`{"$match": {"calcard": "YES"}}`, matching the source CSV's original text
representation instead of the transformed type.

**Fix**: added to the schema card's `conventions`: *"calcard is a BOOLEAN.
Match `{'calcard': true}`, never the string `'YES'`."*

**Where**: `data_pipeline/profile.py`'s `conventions` list.

---

### The frontend parsed zero SSE events

**Symptom**: the most disruptive bug for actually *using* the app. Every
question appeared to hang forever in the UI — no phase indicator, no answer,
nothing — while the backend log showed the request completing successfully
in a normal amount of time (`chat.answered`, `total_ms: 14214`). Waiting
longer never helped, because nothing was actually broken on the server.

**Root cause**: `sse-starlette` (the backend's SSE library) separates event
frames with `\r\n\r\n`. The Angular client's stream parser split incoming
text on a bare `\n\n`, which **never matches** — the receive buffer grew
without bound and not a single event was ever extracted from it. Verified by
running the client's exact parsing logic against the real byte stream: the
old split produced **0 events**; the corrected one produced **18** from the
identical bytes.

The reason this passed every existing test: `backend/tests/test_api.py` and
every manual `curl` check parsed the stream **line by line**, which happens
to tolerate `\r\n` naturally (a line ending in `\r` still starts with
`event:` or `data:`). Only a parser that specifically depends on the blank-line
*frame separator* — which is exactly what the browser client does — was
affected.

**Fix**: `frame.split(/\r?\n\r?\n/)` (and line-splitting on `/\r?\n/`) in
`ChatService.consume()`. Two backend tests were added to pin the literal
byte-level contract going forward
(`test_api.py::TestWireFormat::test_frames_are_separated_by_crlf_crlf`,
`test_every_frame_carries_an_event_and_a_data_line`), since a line-oriented
test alone would not have caught this.

**Where**: `frontend/src/app/core/chat.service.ts::consume`.

---

### Batch runner stopping on a timeout as if it were an outage

**Symptom**: while running the eval harness in batches against a free-tier
model, the runner stopped early — *"the provider is unreachable"* — even
though `/health` showed the model was reachable and plenty of quota
remained.

**Root cause**: the original stop-early logic treated *any* non-passing
result the same way. But a 30-second deadline timeout and a 429 rate limit
are not the same kind of failure: a timeout means the provider **was**
reached and the model was still working when the clock cut it off — a real,
recordable outcome. A 429/402 means the provider was **never** reached at
all — nothing to record, and retrying is certain to fail again.

**Fix**: introduced `is_outage()` (429/402/blank response — leave the case
unrecorded, stop after 2 consecutive occurrences) separately from
`is_timeout()` (record as a failure with a clear reason, keep going to the
next case).

**Where**: `evals/run_eval.py::is_outage`, `is_timeout`.

## Self-corrections worth recording

Two claims made mid-project turned out to be wrong, and the correction
mattered enough to leave a trace here rather than just silently fixing the
next message:

- **"Paid models work now"** — based on a 10-token probe (`max_tokens=10`)
  succeeding against `deepseek/deepseek-v4-flash`. A real call
  (`max_tokens=1500`, what `generate` actually requests) still failed with
  HTTP 402 — the account balance covered small requests but not real ones.
  The probe size mattered; a "yes it's reachable" check at a fraction of the
  real request size is not evidence the real request will succeed.
- **"deepseek scored 26/26"** — true of an earlier run, but that run
  predated the hard per-question deadline being enforced (`asyncio.timeout`
  was added to `routes.py` afterward). Once the deadline existed, deepseek
  had to be re-run under it to be comparable to the free model's result,
  which had always been measured with the deadline in place. The re-run
  scored 22/26 — still the stronger model (zero wrong answers vs. three),
  but the headline number from the first run was stale and not
  apples-to-apples. Both numbers are recorded in
  [`docs/evaluation.md`](evaluation.md) with the caveat attached, rather than
  only the flattering one being kept.

## Deliberate deviations from the spec

Not bugs — decisions to knowingly not match a written requirement, recorded
here so they're never mistaken for oversights.

| Spec value | Shipped value | Why | Detail |
|---|---|---|---|
| `QUESTION_DEADLINE_S` = 30 (SC-004) | 60 | Measured provider latency varies 3–21s *per call*, across three sequential calls per question; 30s cut off answers that were about to succeed | [`docs/operations.md`](operations.md#the-per-question-deadline) |

## Key non-incident decisions

Decisions made deliberately, up front, without a specific bug forcing them.
Full rationale in
[`research.md`](../specs/001-procurement-chat-assistant/research.md); summarized
here for completeness:

- **PyMongo `AsyncMongoClient`, not Motor** — Motor reached end of life
  2026-05-14, before this project started. (Research doc R1.)
- **Allow-list validator, not deny-list** — a stage MongoDB adds in some
  future version is rejected by default, not permitted because nobody
  remembered to add it. (R5, and [`docs/agent.md`](agent.md#the-validator).)
- **`synthesize` sees only rows + question, never the schema or history** —
  makes "no invented figures" a property of the node's available inputs,
  not an instruction the model could disregard. ([`docs/agent.md`](agent.md#synthesize-nodesrespondpy).)
- **Repair budget lives in exactly one function** (`should_repair`) — so the
  3-attempt ceiling and the deadline check can't drift apart between the two
  call sites that need them.
- **`evals/ground_truth.py` imports nothing from `backend/`** — structural,
  not disciplinary, independence. ([`docs/evaluation.md`](evaluation.md#the-independence-guarantee).)
- **Per-model eval result storage** (`evals/results/<slug>.json`) — added
  specifically so two models could be compared side by side instead of the
  second run silently overwriting the first.
