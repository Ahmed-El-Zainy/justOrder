# Evaluation

How the assistant's accuracy is measured, kept honest, and compared across
models. Source: `evals/`. Constitution Principle IV ("evaluated, not
asserted") is what this whole layer exists to satisfy.

## The independence guarantee

`evals/ground_truth.py` computes every expected answer with **pandas,
directly from the source CSV**. It imports nothing from `backend/` — not the
transform, not the agent, not the Mongo client. This is enforced by import
structure, not convention: there is no line of code in `ground_truth.py` that
could reach into the system under test even by accident.

Why this matters concretely: if `data_pipeline/transform.py` had a bug — say,
it mis-parsed `Total Price` — and `evals/ground_truth.py` used that same
transform, the eval would compute its "expected" total with the identical
bug and the wrong answer would score as correct. Because `ground_truth.py`
re-implements money parsing itself (`_money()`, a separate regex-based
parser), a shared bug shows up as a *disagreement*, not a silent pass.

`ground_truth.py` writes `evals/ground_truth.json` — one JSON document with
every fact the golden set needs: `total_line_items`, `distinct_orders`,
`total_spend`, `orders_by_quarter`, `spend_by_quarter`,
`highest_spending_quarter`, `top_items_by_frequency`,
`top_departments_it_services`, `top_suppliers_by_spend`,
`top_departments_by_spend`, `spend_by_acquisition_type`, `orders_by_year`,
`monthly_spend_2014`, `calcard_orders`, `coverage`. Regenerate it with:

```bash
uv run python -m evals.ground_truth
```

## The golden set (`evals/golden_set.yaml`)

26 cases across 11 categories. Each case's `expected_path` is a dotted lookup
into `ground_truth.json` (e.g. `orders_by_quarter.2014-Q3`) — **nothing in
the golden set is a hardcoded number**, so the set stays correct even if the
dataset is reloaded or the CSV changes.

| Category | Cases | What it checks |
|---|---|---|
| `counting` | 4 | Order counts (distinct POs) vs. line-item counts — includes the load-bearing `orders_q3_2014` |
| `ranking` | 4 | Highest-spending quarter, most frequent items |
| `aggregation` | 5 | Spend by department/supplier/acquisition type |
| `combined_filter` | 1 | Category filter + time filter together |
| `time_series` | 1 | Monthly spend across a year |
| `credits` | 1 | Totals are net of the 1,438 negative line items (FR-012) |
| `coverage` | 2 | An out-of-range period is named as such, not queried (FR-015) |
| `empty_result` | 1 | An unknown entity returns "no matching records," never an invented figure (FR-007) |
| `ambiguity` | 1 | A name matching several departments triggers a clarifying question (FR-014a) |
| `out_of_scope` | 2 | Off-topic questions are declined (FR-005) |
| `followup` | 4 | Multi-turn conversation — filter carry-forward and topic-change filter drop (US2) |

Four **match types**, chosen per case for what's actually being tested:

- **`numeric`** — the answer text must contain the expected figure within
  `tolerance`. Used when the specific number is the point.
- **`contains`** — the answer must mention every listed literal string. Used
  when the identity of the answer (which department, which quarter) matters
  more than an exact figure appearing in prose.
- **`absent`** — the answer must **not** contain the listed strings. Used for
  `it_goods_2013_suppliers`, which checks the assistant actually answers a
  combined-filter question rather than failing — the figures themselves are
  already covered by the single-filter cases, so asserting a literal `"$"`
  would only test formatting, not correctness.
- **`refusal`** — the answer must decline, ask, or report emptiness, and
  contain none of a forbidden figure. Used for coverage/empty/ambiguity/
  out-of-scope cases where the *correct* behavior is not answering.

## Running it: batched, because the free tier requires it

`evals/run_eval.py` accumulates results in `evals/results/<model-slug>.json`
— **one file per model** — rather than overwriting a single file on every
run. This exists for a structural reason: each question costs about three
sequential model calls (`understand` → `generate` → `synthesize`), so the
full 26-case set needs roughly 90 calls. OpenRouter's free tier caps at 50
requests/day, so **a complete run cannot fit in a single day on a free
model** — the harness has to support resuming.

```bash
uv run python -m evals.run_eval --status              # what's done, what's left
uv run python -m evals.run_eval --category counting    # run one batch
uv run python -m evals.run_eval                        # run everything outstanding
uv run python -m evals.run_eval --rerun                 # ignore stored results, start over
uv run python -m evals.run_eval --reset                 # discard this model's stored results
uv run python -m evals.run_eval --compare                # write a cross-model comparison
```

Already-recorded case ids are skipped automatically (`outstanding = [case for
... if case["id"] not in store]`) — so re-running the plain command every day
picks up exactly where the previous day left off.

### The distinction that makes batching safe: outage vs. timeout

Two very different "failure" shapes have to be told apart, or the harness
either wastes quota retrying calls that cannot succeed, or silently records
an infrastructure problem as a permanent accuracy figure:

- **Outage** (`is_outage`) — the provider was never reached at all: a 429
  rate limit, a 402 credits-exhausted, or an empty/blank response. This case
  is **left unrecorded** (not saved to the results store at all) so the next
  batch retries it, and the harness stops after **two consecutive** outages
  so it doesn't burn through the rest of a quota that has already died.
- **Timeout** (`is_timeout`) — the model *was* reached and the agent's own
  30/60-second deadline (see
  [`docs/operations.md`](operations.md#the-per-question-deadline)) fired
  before it finished. This **is** a real outcome — the assistant correctly
  gave up rather than hang — so it's recorded as a failure and the batch
  keeps going to the next case.

Conflating these two was a real bug during development: an earlier version
treated every non-outage failure as a reason to stop, so a single slow
question halted a batch that still had quota remaining. See
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md#batch-runner-stopping-on-a-timeout-as-if-it-were-an-outage).

### Reports

Two files, regenerated on every run:

- **`evals/report.md`** — per-model: headline accuracy against SC-001/SC-003/
  SC-004, a per-category breakdown, every failure with its expected vs.
  actual value and why it failed, and a full case-by-case table.
- **`evals/comparison.md`** (via `--compare`) — reads every file under
  `evals/results/` and builds one side-by-side table across all of them,
  splitting **wrong answers** from **timeouts** explicitly (see below for why
  that split matters more than the raw pass count).

## Published results

Two models, same code, same 26 cases, same 30-second deadline (the spec
value, not the 60s shipped default — see
[`docs/operations.md`](operations.md#the-per-question-deadline) for why
those differ):

| Model | Overall | Wrong answers | Timeouts | Accuracy excl. timeouts | Follow-ups |
|---|---|---|---|---|---|
| `deepseek/deepseek-v4-flash` | 22/26 (84.6%) | **0** | 4 | **22/22 (100%)** | 3/4 |
| `google/gemma-4-26b-a4b-it:free` | 21/26 (80.8%) | **3** | 2 | 21/24 (87.5%) | 1/4 |

Full per-case table: [`evals/comparison.md`](../evals/comparison.md).

**Why the wrong/timeout split is the number that matters**: a timeout means
the assistant was still correctly working and got cut off by the clock — the
system behaved exactly as designed (give up rather than guess). A wrong
answer means it returned a confidently incorrect figure. For a system whose
entire premise is grounded answers, these are not equivalent failures.
Deepseek made **zero** wrong answers across the whole set; every one of its
losses was the clock. Gemma made three, two of them follow-ups.

This comparison — and the discovery that the deadline, not model capability,
had become the binding constraint on deepseek's score — is what motivated
raising `QUESTION_DEADLINE_S` from 30 to 60 in the shipped default. See
[`docs/operations.md`](operations.md#the-per-question-deadline) for the
measurements behind that call.

## Bugs the harness found that reading the code didn't

All four fixed; recorded in full in
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md):

1. A boolean field (`calcard`) matched as the literal string `"YES"` instead
   of `true`.
2. "IT services" grounded to `item_name` instead of `acquisition_type`,
   because nothing in the extraction prompt said those category phrases
   were never item names.
3. An out-of-range period was declined as **off-topic** rather than reported
   as **uncovered** — a different, more useful, message for the user.
4. The synthesizer, handed a silent 60-of-90 row slice, summed it and
   presented a total that appeared in **no row** — the single most serious
   finding, since it's exactly the kind of confident-but-wrong figure the
   whole design exists to prevent.
