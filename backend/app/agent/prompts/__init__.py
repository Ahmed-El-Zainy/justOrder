"""Prompt construction: the schema card and the few-shot examples."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_CARD_PATH = Path(__file__).resolve().parents[4] / "data_pipeline" / "schema_card.json"


@lru_cache
def schema_card() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_CARD_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def render_schema() -> str:
    """The dataset description the generator sees."""
    card = schema_card()
    if not card:
        return "Schema unavailable."

    coverage = card.get("coverage", {})
    lines: list[str] = [
        f"COLLECTION: {card['collection']}",
        f"DOCUMENTS: {card['document_count']:,}",
        "",
        "!! CRITICAL !!",
        card.get("grain_warning", ""),
        "",
        "COVERAGE:",
        f"  {coverage.get('from')} to {coverage.get('to')} "
        f"(fiscal years {', '.join(coverage.get('fiscal_years', []))})",
        f"  {coverage.get('note', '')}",
        "",
        "FIELDS:",
    ]

    for field in card.get("fields", []):
        nullable = " (often null)" if field.get("nullable") else ""
        lines.append(
            f"  {field['name']}: {field['type']}{nullable} — {field.get('description', '')}"
        )

    lines.append("")
    lines.append("EXACT VALUES (match verbatim — do not tidy spelling or casing):")
    for name, values in card.get("vocabularies", {}).items():
        if len(values) <= 30:
            lines.append(f"  {name}: {json.dumps(values)}")

    lines.append("")
    lines.append("EXAMPLE VALUES:")
    for name, values in card.get("samples", {}).items():
        lines.append(f"  {name}: {json.dumps(values[:6])}")

    lines.append("")
    lines.append("CONVENTIONS:")
    for rule in card.get("conventions", []):
        lines.append(f"  - {rule}")

    return "\n".join(lines)


# Each example pairs a question with the pipeline it should produce. The first
# two matter most: they contrast counting orders with counting line items.
FEW_SHOT: list[dict[str, Any]] = [
    {
        "question": "How many orders were created in Q3 2014?",
        "pipeline": [
            {"$match": {"creation_quarter_label": "2014-Q3"}},
            {"$group": {"_id": "$purchase_order_number"}},
            {"$count": "orders"},
        ],
        "explanation": "Counts distinct purchase orders created in calendar Q3 2014.",
        "expected_shape": "scalar",
    },
    {
        "question": "How many line items were ordered in 2014?",
        "pipeline": [{"$match": {"creation_year": 2014}}, {"$count": "line_items"}],
        "explanation": "Counts line items — this question asks about items, not orders.",
        "expected_shape": "scalar",
    },
    {
        "question": "Which quarter had the highest spending?",
        "pipeline": [
            {"$match": {"creation_quarter_label": {"$ne": None}}},
            {"$group": {"_id": "$creation_quarter_label", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 12},
        ],
        "explanation": "Total spend per calendar quarter, highest first.",
        "expected_shape": "category_measure",
    },
    {
        "question": "What are the most frequently ordered line items?",
        "pipeline": [
            {"$match": {"item_name_normalized": {"$ne": None}}},
            {"$group": {"_id": "$item_name_normalized", "times_ordered": {"$sum": 1}}},
            {"$sort": {"times_ordered": -1}},
            {"$limit": 10},
        ],
        "explanation": "Ranks items by how many line items name them.",
        "expected_shape": "category_measure",
    },
    {
        "question": "Which departments spent the most on IT services?",
        "pipeline": [
            {"$match": {"acquisition_type": "IT Services"}},
            {"$group": {"_id": "$department_name", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 10},
        ],
        "explanation": "Spend per department, filtered to the IT Services acquisition type.",
        "expected_shape": "category_measure",
    },
    {
        "question": "What did the Department of Transportation spend in fiscal year 2014-2015?",
        "pipeline": [
            {
                "$match": {
                    "department_name": "Transportation, Department of",
                    "fiscal_year": "2014-2015",
                }
            },
            {"$group": {"_id": None, "spend": {"$sum": "$total_price"}}},
        ],
        "explanation": "Total spend for one department in one fiscal year.",
        "expected_shape": "scalar",
    },
    {
        "question": "Show monthly spending in 2014.",
        "pipeline": [
            {"$match": {"creation_year": 2014}},
            {"$group": {"_id": "$creation_month", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"_id": 1}},
        ],
        "explanation": "Spend per calendar month across 2014.",
        "expected_shape": "time_series",
    },
    {
        "question": "Who are the top suppliers by spend for IT goods in 2013?",
        "pipeline": [
            {"$match": {"acquisition_type": "IT Goods", "creation_year": 2013}},
            {"$group": {"_id": "$supplier_name", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 10},
        ],
        "explanation": "Combines a category filter with a time filter.",
        "expected_shape": "category_measure",
    },
    {
        "question": "How many orders used a CalCard?",
        "pipeline": [
            {"$match": {"calcard": True}},
            {"$group": {"_id": "$purchase_order_number"}},
            {"$count": "orders"},
        ],
        "explanation": "Distinct orders where a CalCard was used.",
        "expected_shape": "scalar",
    },
    {
        "question": "What was the average line item value by acquisition method?",
        "pipeline": [
            {"$group": {"_id": "$acquisition_method", "average": {"$avg": "$total_price"}}},
            {"$sort": {"average": -1}},
        ],
        "explanation": "Mean line item value per acquisition method.",
        "expected_shape": "category_measure",
    },
]


def render_examples() -> str:
    blocks: list[str] = []
    for example in FEW_SHOT:
        answer = {key: value for key, value in example.items() if key != "question"}
        blocks.append(f"Q: {example['question']}\nA: {json.dumps(answer)}")
    return "\n\n".join(blocks)


GENERATOR_SYSTEM = """You translate procurement questions into MongoDB aggregation pipelines.

{schema}

EXAMPLES:

{examples}

Return ONLY a JSON object with these keys:
  "collection": always "purchase_orders"
  "pipeline": the aggregation pipeline, as a JSON array of stage objects
  "explanation": one sentence describing what the pipeline computes
  "expected_shape": one of "scalar", "category_measure", "time_series", "rows"

Rules:
- Count ORDERS with $group on purchase_order_number followed by $count. Counting
  documents instead counts line items and overstates the answer by about 73%.
- Use the precomputed period fields. Never recompute quarters from dates.
- Match enum values exactly as listed above.
- Never use $lookup, $out, $merge, $where, $function or $graphLookup. They are
  rejected before execution and waste an attempt.
- Results are capped at 200 rows regardless of what you write."""


UNDERSTAND_SYSTEM = """You prepare procurement questions for querying.

Given the conversation so far and a new message, return ONLY a JSON object:
  "resolved_question": the new message rewritten to stand alone, with any
      reference to earlier turns made explicit. If it already stands alone,
      repeat it unchanged.

      A follow-up inherits EVERYTHING the previous question established that it
      does not itself override — the measure, the year, the calendar basis, and
      any category or department filter. Carry those forward explicitly.

      Worked examples:
        Previous: "What was total spending in Q1 2014?"
        New: "What about Q2?"
        -> "What was total spending in Q2 2014?"        (year and measure kept)

        Previous: "What was total spending in Q2 2014?"
        New: "Break that down by department"
        -> "What was total spending by department in Q2 2014?"

        Previous: "What was total spending by department in Q2 2014?"
        New: "Only the top five"
        -> "What were the top five departments by total spending in Q2 2014?"

        Previous: "What was total spending in Q2 2014?"
        New: "Who are the biggest suppliers?"
        -> "Who are the biggest suppliers by total spending?"
           (a new topic — the Q2 2014 filter is NOT carried over)
  "intent": one of
      "data"        - answerable by querying the procurement dataset
      "schema"      - about what the dataset contains, not about its values
      "out_of_scope"- not about California state procurement at all
      "ambiguous"   - genuinely cannot tell what is being asked
  "entities": object mapping field name to the value the user named, for any of
      department_name, supplier_name, acquisition_type, acquisition_method,
      sub_acquisition_type, item_name. Omit fields the user did not name.

      "IT services", "IT goods", "IT telecommunications", "non-IT goods" and
      "non-IT services" are ALWAYS acquisition_type — they are purchase
      categories, never item names. Use item_name only for a physical thing
      someone ordered, such as "toner" or "medical supplies".

  "period": object with "start" and "end" as ISO dates when the question names a
      specific period, otherwise null.

A procurement question about a period the dataset does not cover is still
"data" — fill in "period" and let the system decide whether it is in range.
Reserve "out_of_scope" for questions that are not about California state
procurement at all.

When the user changes topic, do NOT carry filters over from earlier turns."""


SYNTHESIZER_SYSTEM = """You state what query results show, for a procurement analyst.

You are given a question and the rows a database query returned. Every figure in
your answer must be COPIED from those rows.

You must not calculate. Do not sum, average, subtract, or round the values to
produce a figure that is not already present in a row. If the question asks for
a total and no row contains that total, say what the rows do show — for example
the highest few — and say that a combined total was not part of the result.
Inventing a plausible aggregate is the worst thing you can do here, because it
is indistinguishable from a correct answer to the person reading it.

- Lead with the direct answer in one sentence.
- Quote money exactly as given, formatted as US dollars with thousands
  separators. Do not round.
- Name the filters and the time basis that were applied.
- If you were shown only some of the rows, describe only those and say so.
- Two or three sentences. No preamble, no bullet lists, no markdown headings."""
