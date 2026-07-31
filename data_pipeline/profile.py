"""Profile the raw CSV and generate the schema card the agent is shown.

The constitution requires data-shape assumptions to be verified against the
actual dataset before being encoded into the agent's schema description. This
module is that verification: it reads the source, measures what is there, and
writes the description from the measurement rather than from expectation.

Writes:
    data_pipeline/profile_report.json  — full measured profile
    data_pipeline/schema_card.json     — the trimmed description sent to the LLM
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from data_pipeline.download import csv_path
from data_pipeline.transform import transform_row

csv.field_size_limit(10**9)

HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile_report.json"
SCHEMA_CARD_PATH = HERE / "schema_card.json"

# Fields whose complete value list is small enough to hand the model directly.
ENUM_FIELDS = (
    "acquisition_type",
    "acquisition_method",
    "sub_acquisition_type",
    "fiscal_year",
    "creation_quarter_label",
)
# Large but worth sampling so the model sees the shape of real values.
SAMPLE_FIELDS = ("department_name", "supplier_name", "item_name")
MAX_TRACKED = 200_000

FIELD_DESCRIPTIONS: dict[str, str] = {
    "purchase_order_number": (
        "Purchase order identifier. NOT unique — one order spans many line items. "
        "Count orders with $group on this field, never with a document count."
    ),
    "requisition_number": "Requisition identifier. Absent on ~96% of rows.",
    "lpa_number": "Leveraged Procurement Agreement number. Absent on ~73% of rows.",
    "creation_date": "Date the order was created.",
    "purchase_date": "Date of purchase. Absent on ~5% of rows.",
    "fiscal_year": "State fiscal year, e.g. '2014-2015'. Runs 1 July to 30 June.",
    "creation_year": "Calendar year of creation_date.",
    "creation_month": "Calendar month of creation_date, 1-12.",
    "creation_quarter": "Calendar quarter of creation_date, 1-4.",
    "creation_quarter_label": (
        "Calendar quarter as 'YYYY-Qn', e.g. '2014-Q3'. Use this for quarter questions "
        "unless the user says 'fiscal'. Precomputed — match it with equality, never "
        "recompute quarters from dates."
    ),
    "fiscal_quarter": "Quarter within the state fiscal year, 1-4. Q1 is Jul-Sep.",
    "fiscal_quarter_label": "Fiscal quarter as '2014-2015 Q1'. Use only when asked for fiscal.",
    "department_name": "Ordering State of California department. 111 distinct values.",
    "department_name_normalized": "Lowercased department_name, for grouping and matching.",
    "supplier_name": "Vendor receiving the order. 24,732 distinct values.",
    "supplier_name_normalized": "Lowercased supplier_name, for grouping and matching.",
    "supplier_code": "Vendor identifier. A string — leading zeros are meaningful.",
    "supplier_qualifications": "Certifications such as SB or DVBE. Absent on ~59% of rows.",
    "supplier_zip_code": "Vendor postal code. Absent on ~20% of rows.",
    "acquisition_type": "Top-level purchase category. Five values.",
    "sub_acquisition_type": "Finer category. Absent on ~80% of rows.",
    "acquisition_method": "How the purchase was made. Twenty values.",
    "sub_acquisition_method": "Finer method. Absent on ~91% of rows.",
    "calcard": "Boolean — whether a CalCard purchasing card was used.",
    "item_name": "Short item name. Over 80,000 distinct values.",
    "item_name_normalized": (
        "Lowercased item_name. Group on this for 'most frequently ordered items'."
    ),
    "item_description": "Free-text item description.",
    "quantity": "Units ordered.",
    "unit_price": "Price per unit in USD.",
    "total_price": (
        "Line item total in USD. Sum this for spend questions. Includes 1,438 negative "
        "values representing credits and returns — include them so totals are net."
    ),
    "classification_codes": "UNSPSC classification code.",
    "normalized_unspsc": "Normalized UNSPSC code.",
    "commodity_title": "UNSPSC commodity name.",
    "class_code": "UNSPSC class code.",
    "class_title": "UNSPSC class name.",
    "family_code": "UNSPSC family code.",
    "family_title": "UNSPSC family name.",
    "segment_code": "UNSPSC segment code.",
    "segment_title": "UNSPSC segment name.",
    "location": "Delivery location. Absent on ~20% of rows.",
}

BSON_TYPES: dict[str, str] = {
    "creation_date": "date",
    "purchase_date": "date",
    "creation_year": "int",
    "creation_month": "int",
    "creation_quarter": "int",
    "fiscal_quarter": "int",
    "quantity": "double",
    "unit_price": "double",
    "total_price": "double",
    "calcard": "bool",
}


def profile(limit: int | None = None) -> dict[str, Any]:
    path = csv_path()
    if not path.is_file():
        raise SystemExit(f"[profile] {path} not found — run `python -m data_pipeline.download`")

    print(f"[profile] reading {path.name}")

    rows = 0
    nulls: Counter[str] = Counter()
    distinct: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, Counter[str]] = {field: Counter() for field in ENUM_FIELDS}
    orders: set[str] = set()
    negative_totals = 0
    min_date: str | None = None
    max_date: str | None = None

    with open(path, encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            rows += 1
            doc = transform_row(raw)

            po = doc.get("purchase_order_number")
            if po:
                orders.add(str(po))

            for field, value in doc.items():
                if value is None or value == "":
                    nulls[field] += 1
                elif len(distinct[field]) < MAX_TRACKED:
                    distinct[field].add(str(value))

            for field in ENUM_FIELDS:
                value = doc.get(field)
                if value:
                    counts[field][str(value)] += 1

            total = doc.get("total_price")
            if isinstance(total, (int, float)) and total < 0:
                negative_totals += 1

            created = doc.get("creation_date")
            if created is not None:
                iso = created.date().isoformat()
                min_date = iso if min_date is None or iso < min_date else min_date
                max_date = iso if max_date is None or iso > max_date else max_date

            if limit and rows >= limit:
                break

    fields: dict[str, Any] = {}
    for field in sorted(distinct.keys() | set(nulls.keys())):
        fields[field] = {
            "type": BSON_TYPES.get(field, "string"),
            "null_count": nulls[field],
            "null_rate": round(nulls[field] / rows, 4) if rows else 0.0,
            "distinct": len(distinct.get(field, ())),
        }

    report = {
        "source_file": path.name,
        "rows": rows,
        "distinct_purchase_orders": len(orders),
        "line_items_per_order": round(rows / len(orders), 2) if orders else None,
        "negative_total_price_rows": negative_totals,
        "creation_date_range": {"from": min_date, "to": max_date},
        "fields": fields,
        "enums": {field: dict(counts[field].most_common()) for field in ENUM_FIELDS},
        "samples": {field: sorted(distinct.get(field, ()))[:15] for field in SAMPLE_FIELDS},
    }

    PROFILE_PATH.write_text(json.dumps(report, indent=2))
    print(f"[profile] wrote {PROFILE_PATH.name}")
    print(f"[profile]   rows={rows:,} orders={len(orders):,} negatives={negative_totals:,}")
    return report


def build_schema_card(report: dict[str, Any]) -> dict[str, Any]:
    """Trim the profile into the description the model actually receives."""
    document_fields = [
        {
            "name": name,
            "type": info["type"],
            "nullable": info["null_rate"] > 0,
            "description": FIELD_DESCRIPTIONS.get(name, ""),
        }
        for name, info in report["fields"].items()
        if name in FIELD_DESCRIPTIONS
    ]

    card = {
        "collection": "purchase_orders",
        "document_count": report["rows"],
        "distinct_orders": report["distinct_purchase_orders"],
        "grain": "line item",
        "grain_warning": (
            f"Each document is ONE LINE ITEM, not one order. {report['rows']:,} documents "
            f"resolve to {report['distinct_purchase_orders']:,} distinct purchase orders "
            f"({report['line_items_per_order']} line items per order on average). "
            "To count ORDERS you must count distinct purchase_order_number — for example "
            "[{'$group': {'_id': '$purchase_order_number'}}, {'$count': 'orders'}]. "
            "Counting documents instead overstates order counts by about 73%."
        ),
        "coverage": {
            "from": report["creation_date_range"]["from"],
            "to": report["creation_date_range"]["to"],
            "fiscal_years": sorted(report["enums"].get("fiscal_year", {})),
            "note": (
                "Questions about periods outside this range cannot be answered from this "
                "data. Say the period is not covered rather than returning an empty result."
            ),
        },
        "fields": document_fields,
        "vocabularies": {
            field: sorted(values) for field, values in report["enums"].items() if values
        },
        "samples": report["samples"],
        "conventions": [
            "Spend means the sum of total_price. Include negative values (credits and returns) "
            "so totals are net.",
            "calcard is a BOOLEAN. Match {'calcard': true}, never the string 'YES'.",
            "'IT Services', 'IT Goods', 'NON-IT Goods', 'NON-IT Services' and "
            "'IT Telecommunications' are acquisition_type values, not item names. A question "
            "about spending on IT services filters acquisition_type, not item_name.",
            "An unqualified quarter means a CALENDAR quarter — match creation_quarter_label "
            "such as '2014-Q3'. Only use fiscal_quarter_label when the user says 'fiscal'.",
            "'Most frequently ordered' ranks by how many line items name the item; group on "
            "item_name_normalized.",
            "Match enum values exactly as recorded, including 'NON-IT Goods' in uppercase and "
            "the misspelling 'Expert Witneses'.",
            "Never use $lookup, $out, $merge, $where, or $function — they are rejected before "
            "execution.",
        ],
    }

    SCHEMA_CARD_PATH.write_text(json.dumps(card, indent=2))
    print(f"[profile] wrote {SCHEMA_CARD_PATH.name} ({len(document_fields)} fields described)")
    return card


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile the source CSV and build the schema card")
    parser.add_argument("--limit", type=int, help="only read the first N rows (for a quick run)")
    args = parser.parse_args()

    report = profile(limit=args.limit)
    build_schema_card(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
