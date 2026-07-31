# California Procurement Assistant

![Python](https://img.shields.io/badge/-Python%203.13-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/-FastAPI-009688?style=flat&logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/-LangGraph-1C3C3C?style=flat&logo=langchain&logoColor=white)
![MongoDB](https://img.shields.io/badge/-MongoDB-47A248?style=flat&logo=mongodb&logoColor=white)
![Angular](https://img.shields.io/badge/-Angular%2022-DD0031?style=flat&logo=angular&logoColor=white)
![TypeScript](https://img.shields.io/badge/-TypeScript-3178C6?style=flat&logo=typescript&logoColor=white)
![Docker](https://img.shields.io/badge/-Docker-2496ED?style=flat&logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/tests-183%20passing-brightgreen?style=flat)

A conversational assistant over the State of California large-purchases dataset. You ask a question
in plain English; it writes a MongoDB aggregation pipeline, validates that pipeline against a
deterministic allow-list, runs it read-only, and answers from the rows that came back — showing you
the query it ran.

```
"How many orders were created in Q3 2014?"
  → 18,352 orders were created in Q3 2014.
     [pipeline: $match creation_quarter_label "2014-Q3" → $group _id purchase_order_number → $count]
```

## Table of contents

1. [The detail that decides whether this works](#the-detail-that-decides-whether-this-works)
2. [Architecture](#architecture)

   2.1. [Three decisions that carry the accuracy](#three-decisions-that-carry-the-accuracy)
3. [Running it](#running-it)
4. [Evaluation](#evaluation)

   4.1. [Published scores](#published-scores)

   4.2. [Where the time goes](#where-the-time-goes)
5. [What it does when it can't answer](#what-it-does-when-it-cant-answer)
6. [Layout](#layout)
7. [Tests](#tests)

---

## The detail that decides whether this works

Each row of the source CSV is a **line item, not an order**. 346,018 rows resolve to **200,533
distinct purchase orders** — one order can carry up to 602 line items.

So "how many orders were created in Q3 2014?" is `$group` on `purchase_order_number`, not a
document count. Counting documents returns 29,018 instead of 18,352 — **73% too high**, with a
pipeline that looks entirely reasonable. This single fact drove the schema card, the few-shot
examples, and the first case in the golden set.

---

## Architecture

```
Angular 22 ── SSE ──► FastAPI ──► LangGraph agent ──► MongoDB (read-only user)
   signals              │              │
   chart + table        │              ├─ understand    resolve follow-ups, classify intent
   pipeline panel       │              ├─ ground        match names to values that exist
                        │              ├─ generate      question → aggregation pipeline
                        │              ├─ validate      allow-list, no model in this path
                        │              ├─ execute       maxTimeMS, allowDiskUse
                        │              ├─ repair        ≤3 attempts, bounded by deadline
                        │              └─ synthesize    prose from the rows alone
                        │
                   evals/ ──► pandas ground truth, never touches MongoDB
```

### Three decisions that carry the accuracy

**Precomputed periods.** `creation_quarter_label` is materialised at load time as `2014-Q3`. Asking
a model to express "Q3 2014" as `$dateTrunc` arithmetic invites boundary errors that produce a
plausible wrong number. An equality match either hits or it doesn't.

**Entity grounding before generation.** A user types "Department of Consumer Affairs"; the database
holds "Consumer Affairs, Department of". A model guessing the literal string matches nothing and
the assistant confidently reports zero. Names are resolved against actual stored values first —
including the source's own misspelling, `Expert Witneses`. Where a name matches several departments
("Corrections" matches two), the assistant asks instead of guessing.

**A validator with no model in it.** Every generated pipeline passes a pure-Python allow-list before
it reaches the driver. `$out`, `$merge`, `$where`, `$function`, `$accumulator`, `$graphLookup`,
`$lookup` and `$unionWith` are rejected, including when nested inside a `$facet`. The runtime
MongoDB credential separately holds `read` only, so neither control depends on the other.

---

## Running it

**Prerequisites**: Docker, Python 3.13 + `uv`, Node 22+, and an OpenRouter API key.

```bash
cp .env.example .env          # then set OPENROUTER_API_KEY
docker compose up -d mongo    # or: docker-compose up -d mongo

uv run python -m data_pipeline.download   # fetches the CSV (skipped if present)
uv run python -m data_pipeline.profile    # measures the data, writes the schema card
uv run python -m data_pipeline.load       # loads and verifies

cd backend && uv run uvicorn app.main:app --port 8000
cd frontend && npm install && npx ng serve
```

Frontend on http://localhost:4200, API on http://localhost:8000, `/health` reports document count
and model reachability.

`uv run python -m data_pipeline.load --verify` asserts exactly 346,018 documents, 200,533 distinct
orders, 11 indexes, 6 vocabulary fields, typed doubles, and 1,438 surviving negative rows.

---

## Evaluation

Expected answers are computed in pandas straight from the CSV by `evals/ground_truth.py`, which
imports nothing from `backend/` and never queries MongoDB. That independence is structural: a bug
shared by the transform and a generated pipeline shows up as a disagreement instead of being
reproduced identically on both sides.

```bash
uv run python -m evals.ground_truth
uv run python -m evals.run_eval --status        # what is done, what is left
uv run python -m evals.run_eval --category counting
uv run python -m evals.run_eval                 # everything still outstanding
```

Results accumulate in `evals/results.json`, so the set can be run in batches
across several days. This is not optional on the free tier: each question costs
about three model calls, so 26 cases need roughly 90 against a 50/day
allowance — about 17 cases per day.

A case whose model call fails is left **unrecorded** rather than scored, and the
run stops after two consecutive failures so the remaining quota is not burned.
Recording an outage as a wrong answer would quietly turn a quota problem into a
permanent accuracy figure.

### Published scores

Both models, same code, same 26-case golden set, same 30-second deadline. Full
per-case table in [`evals/comparison.md`](evals/comparison.md).

| Model | Overall | Wrong answers | Timeouts | Excl. timeouts | Follow-ups | Median/turn |
|---|---|---|---|---|---|---|
| `deepseek/deepseek-v4-flash` | 22/26 (84.6%) | **0** | 4 | **22/22 (100%)** | 3/4 | 17.5s |
| `google/gemma-4-26b-a4b-it:free` | 21/26 (80.8%) | **3** | 2 | 21/24 (87.5%) | 1/4 | 21.1s |

The two failure kinds are not equivalent. A **timeout** means the model was
still working when the deadline stopped it — the system behaved correctly and
said it could not answer. A **wrong answer** is the failure that costs trust.

On that measure deepseek got nothing wrong across the whole set; every one of
its four failures was the clock. The free model returned three answers that
were actually incorrect, two of them follow-ups.

**The comparison above was run at the 30-second deadline SC-004 specifies**, and
at that value the deadline was the binding constraint — it cost deepseek four
otherwise-correct answers. Measuring where the time actually goes (below) showed
why, and the deadline has since been raised to 60s in the shipped default.
`QUESTION_DEADLINE_S=30` restores the spec value for anyone who wants to
reproduce the comparison exactly.

`LLM_MODEL` selects the model; nothing else changes.

### Where the time goes

Every answer logs a per-node breakdown, so "it is slow" is attributable rather
than an opaque wait:

```json
{"event": "chat.answered", "total_ms": 12314, "mongo_ms": 109,
 "node_ms": {"understand": 6212, "generate": 3311, "synthesize": 2680}}
```

A representative question:

| Stage | Time | Share |
|---|---|---|
| `understand` (model) | 3–6 s | resolve the question, classify intent |
| `generate` (model) | 3–10 s | write the aggregation pipeline |
| `synthesize` (model) | 2–3 s | write the answer from the rows |
| `execute` (**MongoDB**) | **0.06–0.4 s** | **under 1% of the total** |

The database is not the bottleneck and never was. The cost is three sequential
model calls, and the provider is highly variable — three identical `generate`
requests measured 3.4 s, 20.7 s and 6.3 s with nothing else changed. No prompt
tuning survives that kind of swing, which is why the fix was the deadline
rather than the prompt.

**SC-004 asks for a 30-second ceiling; the shipped default is 60.** Thirty
seconds sat below the observed worst case and cut off answers that were about
to succeed — the deviation is deliberate and recorded here, not hidden.

The structural fix, not yet done, is to stop making three calls: on a first
turn `understand` has no history to resolve against, so folding intent and
entity extraction into `generate` would remove roughly a third of the latency.

The eval harness found four real bugs that reading the code did not: a boolean field matched as the
string `"YES"`, "IT services" grounded to `item_name` instead of `acquisition_type`, an
out-of-range period declined as off-topic rather than reported as uncovered, and a synthesizer that
summed a partial row slice into a total that appeared in no row.

---

## What it does when it can't answer

| Situation | Response |
|---|---|
| Query returns nothing | Says so plainly. No invented figure. |
| Period outside 2012–2015 | Names the covered range. Not an empty table. |
| Name matches several departments | Lists them and asks which. |
| Not about procurement | Declines and says what it can answer. |
| No valid query after 3 repairs | Says it couldn't, and suggests rephrasing. |
| Model provider unreachable | Says the model couldn't be reached — not "no valid query". |

---

## Layout

```
data_pipeline/   download, profile, transform, load — offline, never on the serving path
backend/app/     api/ agent/{nodes,prompts} db/ models/
frontend/src/    chat/ insight/ core/
evals/           golden_set.yaml, ground_truth.py, run_eval.py, report.md
docs/            architecture, agent internals, API reference, ops, bug log — see docs/README.md
specs/           the spec, plan, contracts and tasks this was built from
```

Built spec-first with [Spec Kit](https://github.com/github/spec-kit): the constitution, spec, plan,
contracts and task list in `specs/` came before the code, and `/speckit-analyze` caught a
requirement (FR-015, uncovered periods) that had a validation task but no implementation task.

**For depth beyond this README** — how the agent graph actually routes, the full API/SSE contract,
the data transformation rules, and a bug-by-bug log of everything found by running the system
rather than reading it — see [`docs/README.md`](docs/README.md).

## Tests

```bash
cd backend && uv run pytest tests -v      # 183 tests
```

`tests/test_guards.py` is a merge gate: it asserts every forbidden aggregation stage is rejected,
including nested inside a `$facet` where a top-level check would wave it through.
