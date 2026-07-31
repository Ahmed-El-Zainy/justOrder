"""Index declarations, shared by the loader and the application.

Declared as data rather than as imperative calls so the loader creates exactly
what `--verify` asserts, with no chance of the two lists drifting apart.
"""

from __future__ import annotations

from pymongo import ASCENDING, IndexModel

# (name, keys) for purchase_orders. Eleven indexes covering the query shapes the
# agent actually produces: period filters, party and category aggregation,
# item-frequency ranking, and distinct-order counting.
PURCHASE_ORDER_INDEXES: list[tuple[str, list[tuple[str, int]]]] = [
    ("idx_creation_date", [("creation_date", ASCENDING)]),
    ("idx_creation_quarter_label", [("creation_quarter_label", ASCENDING)]),
    ("idx_fiscal_year", [("fiscal_year", ASCENDING)]),
    ("idx_department_name", [("department_name", ASCENDING)]),
    ("idx_supplier_name", [("supplier_name", ASCENDING)]),
    ("idx_acquisition_type", [("acquisition_type", ASCENDING)]),
    ("idx_acquisition_method", [("acquisition_method", ASCENDING)]),
    ("idx_item_name_normalized", [("item_name_normalized", ASCENDING)]),
    ("idx_purchase_order_number", [("purchase_order_number", ASCENDING)]),
    (
        "idx_creation_date_department",
        [("creation_date", ASCENDING), ("department_name", ASCENDING)],
    ),
    (
        "idx_acq_type_quarter",
        [("acquisition_type", ASCENDING), ("creation_quarter_label", ASCENDING)],
    ),
]

VOCABULARY_INDEXES: list[tuple[str, list[tuple[str, int]]]] = [
    ("idx_field_value_norm", [("field", ASCENDING), ("value_normalized", ASCENDING)]),
    ("idx_field", [("field", ASCENDING)]),
]

EXPECTED_INDEX_COUNT = len(PURCHASE_ORDER_INDEXES)


def purchase_order_index_models() -> list[IndexModel]:
    return [IndexModel(keys, name=name) for name, keys in PURCHASE_ORDER_INDEXES]


def vocabulary_index_models() -> list[IndexModel]:
    return [IndexModel(keys, name=name) for name, keys in VOCABULARY_INDEXES]
