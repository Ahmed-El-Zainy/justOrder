"""Expected answers, computed from the source CSV with pandas.

Constitution Principle IV requires ground truth to come from a path independent
of the one under test. This module therefore imports nothing from `backend/`
and never touches MongoDB — the independence is structural, not a matter of
discipline. A bug in the transform or in a generated pipeline shows up here as a
disagreement rather than being reproduced identically on both sides.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "kaggle_data" / "PURCHASE ORDER DATA EXTRACT 2012-2015_0.csv"
OUTPUT_PATH = Path(__file__).resolve().parent / "ground_truth.json"

_PARENS = re.compile(r"^\((.*)\)$")


def _money(series: pd.Series) -> pd.Series:
    """Parse `$1,234.56` and `($50.00)` independently of the app's transform."""
    text = series.astype(str).str.strip()
    negative = text.str.match(_PARENS)
    cleaned = text.str.replace(r"[$,()]", "", regex=True)
    values = pd.to_numeric(cleaned, errors="coerce")
    return values.where(~negative, -values)


def load() -> pd.DataFrame:
    if not CSV_PATH.is_file():
        raise SystemExit(f"[ground_truth] {CSV_PATH} not found — run data_pipeline.download")

    df = pd.read_csv(CSV_PATH, low_memory=False)
    df["_created"] = pd.to_datetime(df["Creation Date"], format="%m/%d/%Y", errors="coerce")
    df["_total"] = _money(df["Total Price"])
    df["_year"] = df["_created"].dt.year
    df["_quarter"] = df["_created"].dt.quarter
    df["_quarter_label"] = (
        df["_year"].astype("Int64").astype(str) + "-Q" + df["_quarter"].astype("Int64").astype(str)
    )
    df["_month"] = df["_created"].dt.month
    df["_item_norm"] = (
        df["Item Name"].astype(str).str.strip().str.lower().str.replace(r"\s+", " ", regex=True)
    )
    return df


def compute(df: pd.DataFrame) -> dict[str, Any]:
    facts: dict[str, Any] = {}

    facts["total_line_items"] = len(df)
    facts["distinct_orders"] = int(df["Purchase Order Number"].nunique())
    facts["total_spend"] = round(float(df["_total"].sum()), 2)
    facts["negative_line_items"] = int((df["_total"] < 0).sum())

    # Orders per quarter — distinct purchase orders, not row counts.
    orders_by_quarter = (
        df.dropna(subset=["_quarter_label"])
        .groupby("_quarter_label")["Purchase Order Number"]
        .nunique()
        .sort_index()
    )
    facts["orders_by_quarter"] = {k: int(v) for k, v in orders_by_quarter.items()}
    facts["line_items_by_quarter"] = {
        k: int(v) for k, v in df.groupby("_quarter_label").size().sort_index().items()
    }

    spend_by_quarter = df.groupby("_quarter_label")["_total"].sum().sort_values(ascending=False)
    facts["spend_by_quarter"] = {k: round(float(v), 2) for k, v in spend_by_quarter.items()}
    facts["highest_spending_quarter"] = {
        "quarter": str(spend_by_quarter.index[0]),
        "spend": round(float(spend_by_quarter.iloc[0]), 2),
    }

    top_items = df.groupby("_item_norm").size().sort_values(ascending=False).head(10)
    facts["top_items_by_frequency"] = [
        {"item": str(k), "count": int(v)} for k, v in top_items.items()
    ]

    it_services = df[df["Acquisition Type"] == "IT Services"]
    dept_spend = it_services.groupby("Department Name")["_total"].sum().sort_values(ascending=False)
    facts["top_departments_it_services"] = [
        {"department": str(k), "spend": round(float(v), 2)} for k, v in dept_spend.head(10).items()
    ]

    supplier_spend = df.groupby("Supplier Name")["_total"].sum().sort_values(ascending=False)
    facts["top_suppliers_by_spend"] = [
        {"supplier": str(k), "spend": round(float(v), 2)}
        for k, v in supplier_spend.head(10).items()
    ]

    dept_spend_all = df.groupby("Department Name")["_total"].sum().sort_values(ascending=False)
    facts["top_departments_by_spend"] = [
        {"department": str(k), "spend": round(float(v), 2)}
        for k, v in dept_spend_all.head(10).items()
    ]

    facts["spend_by_acquisition_type"] = {
        str(k): round(float(v), 2)
        for k, v in df.groupby("Acquisition Type")["_total"]
        .sum()
        .sort_values(ascending=False)
        .items()
    }

    facts["orders_by_year"] = {
        str(int(k)): int(v)
        for k, v in df.dropna(subset=["_year"])
        .groupby("_year")["Purchase Order Number"]
        .nunique()
        .items()
    }

    facts["monthly_spend_2014"] = {
        str(int(k)): round(float(v), 2)
        for k, v in df[df["_year"] == 2014].groupby("_month")["_total"].sum().sort_index().items()
    }

    facts["calcard_orders"] = int(
        df[df["CalCard"].astype(str).str.upper() == "YES"]["Purchase Order Number"].nunique()
    )

    facts["coverage"] = {
        "from": str(df["_created"].min().date()),
        "to": str(df["_created"].max().date()),
    }

    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute expected answers from the source CSV")
    parser.add_argument("--print", action="store_true", help="print a summary")
    args = parser.parse_args()

    df = load()
    facts = compute(df)
    OUTPUT_PATH.write_text(json.dumps(facts, indent=2))
    print(f"[ground_truth] wrote {OUTPUT_PATH.name} ({len(facts)} facts)")

    if args.print:
        print(json.dumps(facts, indent=2)[:3000])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
