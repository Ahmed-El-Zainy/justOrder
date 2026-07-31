"""Convert raw CSV rows into typed MongoDB documents.

The source is text throughout: money arrives as `$1,234.56`, dates as
`MM/DD/YYYY`, booleans as `YES`/`NO`. Every aggregation the agent generates
depends on these being real numbers, dates, and booleans.

Two decisions here carry most of the accuracy weight:

1. Period fields are precomputed. Asking a model to express "Q3 2014" as a
   `$dateTrunc`/`$expr` combination invites off-by-one boundary errors that
   produce a plausible wrong number. `creation_quarter_label == "2014-Q3"` is an
   equality match that cannot be subtly wrong.
2. Rows are never dropped. A row missing every optional field still becomes a
   document with nulls (FR-030), because a silently shrinking dataset is a far
   worse failure than a null.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Source column -> document field. Class/Family/Segment gain a _code suffix so
# each pairs legibly with its _title counterpart.
COLUMN_MAP: dict[str, str] = {
    "Creation Date": "creation_date",
    "Purchase Date": "purchase_date",
    "Fiscal Year": "fiscal_year",
    "LPA Number": "lpa_number",
    "Purchase Order Number": "purchase_order_number",
    "Requisition Number": "requisition_number",
    "Acquisition Type": "acquisition_type",
    "Sub-Acquisition Type": "sub_acquisition_type",
    "Acquisition Method": "acquisition_method",
    "Sub-Acquisition Method": "sub_acquisition_method",
    "Department Name": "department_name",
    "Supplier Code": "supplier_code",
    "Supplier Name": "supplier_name",
    "Supplier Qualifications": "supplier_qualifications",
    "Supplier Zip Code": "supplier_zip_code",
    "CalCard": "calcard",
    "Item Name": "item_name",
    "Item Description": "item_description",
    "Quantity": "quantity",
    "Unit Price": "unit_price",
    "Total Price": "total_price",
    "Classification Codes": "classification_codes",
    "Normalized UNSPSC": "normalized_unspsc",
    "Commodity Title": "commodity_title",
    "Class": "class_code",
    "Class Title": "class_title",
    "Family": "family_code",
    "Family Title": "family_title",
    "Segment": "segment_code",
    "Segment Title": "segment_title",
    "Location": "location",
}

MONEY_FIELDS = ("unit_price", "total_price")
DATE_FIELDS = ("creation_date", "purchase_date")
NORMALIZED_FIELDS = ("department_name", "supplier_name", "item_name")

_WHITESPACE = re.compile(r"\s+")
_MONEY_STRIP = re.compile(r"[$,\s]")


def clean_text(value: Any) -> str | None:
    """Trim to a non-empty string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_name(value: Any) -> str | None:
    """Lowercase, trim, collapse internal whitespace — the grouping/match key."""
    text = clean_text(value)
    if text is None:
        return None
    return _WHITESPACE.sub(" ", text).lower()


def parse_money(value: Any) -> float | None:
    """Parse `$1,234.56`, `($50.00)`, `-$50.00`, `$-50.00` to a float.

    Negatives are credits and returns (1,438 of them in the source). They are
    preserved so that monetary totals come out net, per FR-012.
    """
    text = clean_text(value)
    if text is None:
        return None

    negative = False
    # Accounting notation: (50.00) means -50.00
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    stripped = _MONEY_STRIP.sub("", text)
    if stripped.startswith("-"):
        negative = True
        stripped = stripped[1:]
    # `$-50.00` leaves a leading '-' only after the $ is stripped, handled above.

    if not stripped:
        return None

    try:
        amount = float(stripped)
    except ValueError:
        return None

    return -amount if negative else amount


def parse_number(value: Any) -> float | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return float(_MONEY_STRIP.sub("", text))
    except ValueError:
        return None


def parse_date(value: Any) -> datetime | None:
    """Parse `MM/DD/YYYY`. Anything unparseable becomes None, never a sentinel."""
    text = clean_text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y")
    except ValueError:
        return None


def parse_bool(value: Any) -> bool:
    text = clean_text(value)
    return text is not None and text.upper() == "YES"


def fiscal_year_of(date: datetime) -> str:
    """The California fiscal year begins 1 July: Aug 2014 falls in 2014-2015."""
    if date.month >= 7:
        return f"{date.year}-{date.year + 1}"
    return f"{date.year - 1}-{date.year}"


def fiscal_quarter_of(date: datetime) -> int:
    """Q1 = Jul-Sep, Q2 = Oct-Dec, Q3 = Jan-Mar, Q4 = Apr-Jun."""
    return ((date.month - 7) % 12) // 3 + 1


def derive_periods(doc: dict[str, Any]) -> None:
    """Add the precomputed calendar and fiscal period fields, in place."""
    created = doc.get("creation_date")

    if not isinstance(created, datetime):
        for field in (
            "creation_year",
            "creation_month",
            "creation_quarter",
            "creation_quarter_label",
            "fiscal_quarter",
            "fiscal_quarter_label",
        ):
            doc[field] = None
        return

    quarter = (created.month - 1) // 3 + 1
    doc["creation_year"] = created.year
    doc["creation_month"] = created.month
    doc["creation_quarter"] = quarter
    doc["creation_quarter_label"] = f"{created.year}-Q{quarter}"

    fiscal_quarter = fiscal_quarter_of(created)
    doc["fiscal_quarter"] = fiscal_quarter
    # Prefer the fiscal year the source assigned; fall back to the one implied
    # by the creation date when that column is blank.
    fiscal_year = doc.get("fiscal_year") or fiscal_year_of(created)
    doc["fiscal_quarter_label"] = f"{fiscal_year} Q{fiscal_quarter}"


def transform_row(row: dict[str, Any]) -> dict[str, Any]:
    """Turn one raw CSV row into a typed document.

    Always returns a document — a row with every optional field blank still
    loads, with nulls (FR-030).
    """
    doc: dict[str, Any] = {}

    for source, target in COLUMN_MAP.items():
        doc[target] = clean_text(row.get(source))

    for field in DATE_FIELDS:
        doc[field] = parse_date(doc[field])

    for field in MONEY_FIELDS:
        doc[field] = parse_money(doc[field])

    doc["quantity"] = parse_number(doc["quantity"])
    doc["calcard"] = parse_bool(row.get("CalCard"))

    for field in NORMALIZED_FIELDS:
        doc[f"{field}_normalized"] = normalize_name(doc[field])

    derive_periods(doc)
    return doc
