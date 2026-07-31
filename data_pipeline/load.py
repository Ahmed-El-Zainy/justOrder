"""Load the transformed CSV into MongoDB, build indexes and the vocabulary.

Connects with the admin credential — this is the one place in the project that
needs write access, and it runs offline, never as part of serving a request.

Run:
    python -m data_pipeline.load            # load
    python -m data_pipeline.load --verify   # assert the load is correct
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.db.indexes import (
    EXPECTED_INDEX_COUNT,
    PURCHASE_ORDER_INDEXES,
    VOCABULARY_INDEXES,
)

from data_pipeline.download import csv_path
from data_pipeline.transform import transform_row

csv.field_size_limit(10**9)

MONGO_ADMIN_URI = "mongodb://root:root_pw@localhost:27017/?authSource=admin"
DB_NAME = "procurement"
COLLECTION = "purchase_orders"
VOCAB_COLLECTION = "field_vocabulary"
BATCH_SIZE = 5_000

# Fields small enough to cache in memory for entity grounding. Suppliers
# (24,732) and item names (>80,000) are queried on demand instead.
VOCABULARY_FIELDS = (
    "department_name",
    "acquisition_type",
    "acquisition_method",
    "sub_acquisition_type",
    "fiscal_year",
    "creation_quarter_label",
)

# Measured from the source file, not estimated.
EXPECTED_DOCUMENTS = 346_018
EXPECTED_DISTINCT_ORDERS = 200_533


def _connect(uri: str) -> MongoClient[dict[str, Any]]:
    client: MongoClient[dict[str, Any]] = MongoClient(uri, serverSelectionTimeoutMS=10_000)
    client.admin.command("ping")
    return client


def load(uri: str, drop: bool = True) -> int:
    path = csv_path()
    if not path.is_file():
        sys.exit(f"[load] {path} not found — run `python -m data_pipeline.download` first")

    client = _connect(uri)
    db = client[DB_NAME]
    collection = db[COLLECTION]

    if drop:
        print(f"[load] dropping existing {COLLECTION}")
        collection.drop()

    print(f"[load] reading {path.name}")
    inserted = 0
    batch: list[dict[str, Any]] = []
    vocab: dict[str, Counter[str]] = {field: Counter() for field in VOCABULARY_FIELDS}

    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            doc = transform_row(row)
            batch.append(doc)

            for field in VOCABULARY_FIELDS:
                value = doc.get(field)
                if value:
                    vocab[field][str(value)] += 1

            if len(batch) >= BATCH_SIZE:
                collection.insert_many(batch, ordered=False)
                inserted += len(batch)
                batch.clear()
                if inserted % 50_000 == 0:
                    print(f"[load]   {inserted:,} documents")

    if batch:
        collection.insert_many(batch, ordered=False)
        inserted += len(batch)

    print(f"[load] inserted {inserted:,} documents")

    print("[load] creating indexes")
    for name, keys in PURCHASE_ORDER_INDEXES:
        collection.create_index(keys, name=name)

    _build_vocabulary(db, vocab)

    client.close()
    return inserted


def _build_vocabulary(db: Any, vocab: dict[str, Counter[str]]) -> None:
    """Materialize distinct values so entity grounding matches what is stored."""
    print("[load] building field_vocabulary")
    collection = db[VOCAB_COLLECTION]
    collection.drop()

    entries: list[dict[str, Any]] = [
        {
            "field": field,
            "value": value,
            "value_normalized": value.strip().lower(),
            "count": count,
        }
        for field, counts in vocab.items()
        for value, count in counts.items()
    ]

    if entries:
        collection.insert_many(entries, ordered=False)

    for name, keys in VOCABULARY_INDEXES:
        collection.create_index(keys, name=name)

    summary = ", ".join(f"{field}={len(counts)}" for field, counts in vocab.items())
    print(f"[load]   {len(entries):,} vocabulary entries ({summary})")


def verify(uri: str) -> int:
    """Assert the load is exactly right. Exits non-zero on any mismatch."""
    client = _connect(uri)
    db = client[DB_NAME]
    collection = db[COLLECTION]
    failures: list[str] = []

    count = collection.count_documents({})
    print(f"[verify] documents: {count:,} (expected {EXPECTED_DOCUMENTS:,})")
    if count != EXPECTED_DOCUMENTS:
        failures.append(f"document count {count:,} != {EXPECTED_DOCUMENTS:,}")

    distinct_orders = len(collection.distinct("purchase_order_number"))
    print(
        f"[verify] distinct purchase orders: {distinct_orders:,} "
        f"(expected {EXPECTED_DISTINCT_ORDERS:,})"
    )
    if distinct_orders != EXPECTED_DISTINCT_ORDERS:
        failures.append(f"distinct orders {distinct_orders:,} != {EXPECTED_DISTINCT_ORDERS:,}")

    # The load-bearing distinction: rows are line items, not orders.
    if count and distinct_orders:
        ratio = count / distinct_orders
        print(f"[verify] line items per order: {ratio:.2f}")

    index_names = set(collection.index_information()) - {"_id_"}
    print(f"[verify] indexes: {len(index_names)} (expected {EXPECTED_INDEX_COUNT})")
    missing = {name for name, _ in PURCHASE_ORDER_INDEXES} - index_names
    if missing:
        failures.append(f"missing indexes: {sorted(missing)}")

    vocab_fields = db[VOCAB_COLLECTION].distinct("field")
    print(f"[verify] vocabulary fields: {len(vocab_fields)} (expected {len(VOCABULARY_FIELDS)})")
    missing_vocab = set(VOCABULARY_FIELDS) - set(vocab_fields)
    if missing_vocab:
        failures.append(f"missing vocabulary fields: {sorted(missing_vocab)}")

    typed = collection.find_one({"total_price": {"$type": "double"}})
    if typed is None:
        failures.append("no document has a double total_price — transform did not run")
    else:
        print("[verify] total_price is typed as double")

    negatives = collection.count_documents({"total_price": {"$lt": 0}})
    print(f"[verify] negative total_price rows: {negatives:,} (credits/returns, FR-012)")
    if negatives == 0:
        failures.append("no negative total_price rows — credits were lost in transform")

    client.close()

    if failures:
        print("\n[verify] FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\n[verify] all checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Load the CA procurement dataset into MongoDB")
    parser.add_argument("--verify", action="store_true", help="verify an existing load")
    parser.add_argument("--uri", default=MONGO_ADMIN_URI, help="MongoDB URI with write access")
    parser.add_argument("--keep", action="store_true", help="do not drop the collection first")
    args = parser.parse_args()

    if args.verify:
        return verify(args.uri)

    load(args.uri, drop=not args.keep)
    return verify(args.uri)


if __name__ == "__main__":
    raise SystemExit(main())
