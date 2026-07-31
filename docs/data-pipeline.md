# Data Pipeline

How the raw Kaggle CSV becomes the MongoDB collection the agent queries, and
the schema card the agent is shown. Runs entirely offline, before the server
starts — see [`docs/architecture.md`](architecture.md#why-theyre-separated-this-way)
for why this is a separate package rather than backend startup code.

Source: `data_pipeline/`. Run order: `download` → `profile` → `load`
(`profile` and `load` don't depend on each other, but both need `download`
first, and `profile`'s output is what `load` calls `--verify` against
indirectly — see below).

```bash
uv run python -m data_pipeline.download
uv run python -m data_pipeline.profile
uv run python -m data_pipeline.load
uv run python -m data_pipeline.load --verify
```

## Source data

`kaggle_data/PURCHASE ORDER DATA EXTRACT 2012-2015_0.csv` — 156 MB, 346,018
rows, 31 columns, from
[`sohier/large-purchases-by-the-state-of-ca`](https://www.kaggle.com/datasets/sohier/large-purchases-by-the-state-of-ca).

**`download.py`** fetches it via the `kaggle` CLI (credentials expected at
`~/.kaggle/kaggle.json`) and is idempotent — `already_present()` checks the
file exists and is at least 150 MB (`EXPECTED_MIN_BYTES`) before skipping a
re-download, so a partial/truncated file doesn't get silently treated as
complete.

## Measured facts, not assumed ones

Every number below was read out of the CSV by `profile.py`, not estimated —
the constitution requires data-shape assumptions to be verified before being
encoded into the agent's schema description.

| Fact | Value |
|---|---|
| Rows (line items) | 346,018 |
| Distinct purchase orders | 200,533 |
| Line items per order (average) | 1.73 |
| Line items on the largest single order | 602 |
| Negative `total_price` rows (credits/returns) | 1,438 |
| Distinct departments | 111 |
| Distinct suppliers | 24,732 |
| Distinct item names | >80,000 |
| Acquisition types | 5 |
| Acquisition methods | 20 |
| Date coverage | 2012-07-02 → 2015-06-30 |
| Fiscal years present | 2012-2013, 2013-2014, 2014-2015 |

**The one fact that shapes everything else**: 346,018 rows resolve to only
200,533 distinct orders. Each row is a line item, not an order — a single
purchase order can carry up to 602 of them. A question about "how many
orders" answered by counting documents overstates by about 73%. This is
stated in the generated schema card's `grain_warning` field verbatim, and is
the first case in the evaluation golden set (`orders_q3_2014`).

## Transformation (`transform.py`)

`transform_row()` takes one raw CSV row (a `dict[str, str]`, all values text)
and returns one typed document. It is deliberately pure and side-effect free,
which is what makes it directly unit-testable (`backend/tests/test_transform.py`,
62 cases) independent of any database.

### Column mapping

`COLUMN_MAP` renames all 31 source columns to `snake_case`. Three —
`Class`, `Family`, `Segment` — get a `_code` suffix so each pairs legibly
with its `_title` counterpart (`class_code`/`class_title`, etc.) rather than
colliding with the bare noun.

### Typed parsing

| Rule | Function | Behavior |
|---|---|---|
| Money | `parse_money` | `"$1,234.56"` → `1234.56`. Handles accounting-notation negatives (`"($50.00)"` → `-50.0`) and a leading minus in either position (`"-$50.00"`, `"$-50.00"`). Preserves negatives — they're the 1,438 credits/returns, and FR-012 requires totals be net of them. |
| Dates | `parse_date` | `"MM/DD/YYYY"` → `datetime`. Anything unparseable (blank, malformed) becomes `None` — never a sentinel date that could silently join a range query's results. |
| Booleans | `parse_bool` | Only the literal string `"YES"` (case-insensitive) is `True`; everything else, including blank, is `False`. |
| Numbers | `parse_number` | Same money-stripping regex as `parse_money`, without the sign handling — used for `quantity`. |
| Text | `clean_text` | Trim to a non-empty string or `None`. Every other parser starts here. |

### Normalization

`department_name`, `supplier_name`, `item_name` each get a `_normalized`
companion (`normalize_name`): lowercased, trimmed, internal whitespace
collapsed to single spaces. These are the grouping/matching keys —
`item_name_normalized` is what "most frequently ordered" groups on, so that
`"Toner"` and `"toner  "` count as the same item.

### Derived period fields (`derive_periods`)

Computed once at load time so the agent never has to express date arithmetic
in a generated pipeline — see
[`docs/agent.md`](agent.md#generate-nodesgeneratepy-function-generate_pipeline)
for why that matters.

| Field | Derivation |
|---|---|
| `creation_year`, `creation_month` | Straight from `creation_date` |
| `creation_quarter` | `(month - 1) // 3 + 1` — calendar quarter, 1–4 |
| `creation_quarter_label` | `"{year}-Q{quarter}"`, e.g. `"2014-Q3"` — an equality match, never recomputed |
| `fiscal_quarter` | `((month - 7) % 12) // 3 + 1` — CA fiscal year begins 1 July, so Q1 is Jul–Sep |
| `fiscal_quarter_label` | `"{fiscal_year} Q{n}"`, preferring the source's own `Fiscal Year` column and falling back to `fiscal_year_of(date)` when that column is blank |

Calendar and fiscal quarters genuinely diverge: 15 January 2015 is calendar
`2015-Q1` but fiscal `2014-2015 Q3`. An unqualified "Q3 2014" in a question
means the **calendar** reading (FR-014b) unless the user says "fiscal" — this
convention is stated explicitly in the schema card.

A row with no parseable `creation_date` gets all six derived fields set to
`None` — never a sentinel that would silently match a range filter.

## Loading (`load.py`)

Connects with the **admin** credential (`MONGO_ADMIN_URI`) — the only place
in the whole project that needs write access to MongoDB, and it never runs
as part of serving a request.

1. Drops `purchase_orders` (unless `--keep`).
2. Streams the CSV through `transform_row()` in batches of `BATCH_SIZE = 5,000`,
   `insert_many(..., ordered=False)` — an ordered insert would abort the
   whole batch on one bad document; unordered lets everything else through.
3. Creates all 11 indexes from `app/db/indexes.py` (shared, not duplicated —
   see below).
4. Builds `field_vocabulary` (see [Entity grounding](agent.md#entity-grounding)) by
   counting distinct values seen during the same pass, for the six cached
   fields: `department_name`, `acquisition_type`, `acquisition_method`,
   `sub_acquisition_type`, `fiscal_year`, `creation_quarter_label`.

### `field_vocabulary` collection

One document per `(field, value)` pair actually observed:

```json
{"field": "department_name", "value": "Consumer Affairs, Department of",
 "value_normalized": "consumer affairs, department of", "count": 5000}
```

`count` is the document frequency, used to break ties toward the commoner
reading when two candidates score identically (`vocabulary.py::match_value`).
Indexed on `(field, value_normalized)` and `field` alone
(`VOCABULARY_INDEXES`).

### Indexes (`app/db/indexes.py`)

Declared once, as data, and imported by both `load.py` and the running
application — so `--verify`'s expectation and what actually gets created can
never drift apart from each other.

| Index | Covers |
|---|---|
| `creation_date` | Date-range filters |
| `creation_quarter_label` | Quarter questions |
| `fiscal_year` | Fiscal-year filters |
| `department_name` | Department aggregation |
| `supplier_name` | Supplier aggregation |
| `acquisition_type` | Category filters |
| `acquisition_method` | Method filters |
| `item_name_normalized` | Line-item frequency ranking |
| `purchase_order_number` | Distinct-order counting |
| `(creation_date, department_name)` compound | The common "department spend over a period" shape |
| `(acquisition_type, creation_quarter_label)` compound | Category-filtered period questions |

### `--verify`

Asserts, and exits non-zero on any mismatch:

- exactly **346,018** documents,
- exactly **200,533** distinct `purchase_order_number` values,
- all **11** indexes present by name,
- all **6** vocabulary fields populated,
- at least one document has `total_price` typed as a BSON double (proof the
  transform actually ran, not just a raw string import),
- at least one document has a negative `total_price` (proof credits survived
  the transform rather than being dropped or clamped).

This is the concrete, automatable form of "the load is correct" — not a
visual spot-check.

## Profiling and the schema card (`profile.py`)

Two outputs, from one pass over the CSV:

**`profile_report.json`** — the full measurement: per-field null rate and
distinct count, complete enum value+frequency for the five `ENUM_FIELDS`,
15-value samples for the three high-cardinality `SAMPLE_FIELDS`
(`department_name`, `supplier_name`, `item_name`), min/max creation date, and
the order-vs-line-item ratio.

**`schema_card.json`** — `build_schema_card()` trims that into what the
generation prompt actually receives (`agent/prompts/__init__.py::render_schema()`
reads this file directly). It carries, per field, a **hand-written
description** (`FIELD_DESCRIPTIONS`) alongside the measured type and
nullability — descriptions like *"Purchase order identifier. NOT unique —
one order spans many line items. Count orders with `$group` on this field,
never with a document count."* on `purchase_order_number` are what make the
grain distinction land in the model's actual prompt, not just this
documentation.

The card also carries `conventions` — short, model-facing rules distilled
from real failures during development (money is net of credits; `calcard` is
a boolean, never the string `"YES"`; IT-category phrases are
`acquisition_type` values, not item names; match enums verbatim including the
source's own `"Expert Witneses"` misspelling). Several of these were added
*after* a specific wrong answer was observed — see
[`docs/decisions-and-bugs.md`](decisions-and-bugs.md) for the incidents.

Because `guards.py::_load_known_fields()` also reads `schema_card.json` (for
field-reference validation — see [`docs/agent.md`](agent.md#the-validator)),
regenerating the schema card after any transform change keeps the validator's
notion of "known fields" and the generator's notion of "available fields" in
sync automatically, without duplicated field lists to maintain.

## Reloading after a data or code change

```bash
uv run python -m data_pipeline.profile     # if transform.py changed
uv run python -m data_pipeline.load        # drops and reloads purchase_orders
uv run python -m data_pipeline.load --verify
```

The `field_vocabulary` cache the running backend holds in memory
(`vocabulary.py`'s module-level `_vocabulary` dict) is loaded once at
startup (`main.py`'s `lifespan`). Restart the backend after a reload so it
picks up any new distinct values.
