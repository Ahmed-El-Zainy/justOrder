"""Transform correctness — the layer every generated aggregation depends on.

If money parses wrong, every spend figure is wrong. If quarters derive wrong,
every period question is wrong, and the error is invisible because the answer
still looks reasonable.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data_pipeline.transform import (
    derive_periods,
    fiscal_quarter_of,
    fiscal_year_of,
    normalize_name,
    parse_bool,
    parse_date,
    parse_money,
    parse_number,
    transform_row,
)


class TestParseMoney:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$1.00", 1.00),
            ("$1,234.56", 1234.56),
            ("$1,234,567.89", 1234567.89),
            ("1234.56", 1234.56),
            ("$0.00", 0.0),
        ],
    )
    def test_positive_amounts(self, raw: str, expected: float) -> None:
        assert parse_money(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("($50.00)", -50.0),
            ("-$50.00", -50.0),
            ("$-50.00", -50.0),
            ("($1,234.56)", -1234.56),
        ],
    )
    def test_negatives_are_preserved(self, raw: str, expected: float) -> None:
        """1,438 rows carry credits/returns. FR-012 requires totals be net."""
        assert parse_money(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "   ", None, "N/A", "$"])
    def test_unparseable_becomes_none(self, raw: str | None) -> None:
        assert parse_money(raw) is None


class TestParseDate:
    def test_valid_date(self) -> None:
        assert parse_date("08/27/2013") == datetime(2013, 8, 27)

    def test_end_of_year(self) -> None:
        assert parse_date("12/31/2014") == datetime(2014, 12, 31)

    @pytest.mark.parametrize("raw", ["", "   ", None, "not a date", "2013-08-27", "13/45/2013"])
    def test_unparseable_becomes_none_not_a_sentinel(self, raw: str | None) -> None:
        """A sentinel date would silently join real results in a range query."""
        assert parse_date(raw) is None


class TestParseBool:
    @pytest.mark.parametrize(("raw", "expected"), [("YES", True), ("yes", True), ("Yes", True)])
    def test_yes(self, raw: str, expected: bool) -> None:
        assert parse_bool(raw) is expected

    @pytest.mark.parametrize("raw", ["NO", "no", "", None, "maybe"])
    def test_everything_else_is_false(self, raw: str | None) -> None:
        assert parse_bool(raw) is False


class TestNormalizeName:
    def test_lowercases_and_trims(self) -> None:
        assert normalize_name("  Consumer Affairs, Department of  ") == (
            "consumer affairs, department of"
        )

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_name("Pitney\t\tBowes   Inc") == "pitney bowes inc"

    def test_empty_becomes_none(self) -> None:
        assert normalize_name("   ") is None


class TestFiscalPeriods:
    """The CA fiscal year begins 1 July."""

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (datetime(2014, 7, 1), "2014-2015"),
            (datetime(2014, 8, 27), "2014-2015"),
            (datetime(2014, 12, 31), "2014-2015"),
            (datetime(2015, 6, 30), "2014-2015"),
            (datetime(2014, 6, 30), "2013-2014"),
        ],
    )
    def test_fiscal_year_boundary(self, date: datetime, expected: str) -> None:
        assert fiscal_year_of(date) == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (datetime(2014, 7, 15), 1),
            (datetime(2014, 9, 30), 1),
            (datetime(2014, 10, 1), 2),
            (datetime(2014, 12, 31), 2),
            (datetime(2015, 1, 1), 3),
            (datetime(2015, 3, 31), 3),
            (datetime(2015, 4, 1), 4),
            (datetime(2015, 6, 30), 4),
        ],
    )
    def test_fiscal_quarter(self, date: datetime, expected: int) -> None:
        assert fiscal_quarter_of(date) == expected


class TestDerivePeriods:
    def test_calendar_and_fiscal_from_one_date(self) -> None:
        doc = {"creation_date": datetime(2014, 8, 27), "fiscal_year": "2014-2015"}
        derive_periods(doc)

        assert doc["creation_year"] == 2014
        assert doc["creation_month"] == 8
        assert doc["creation_quarter"] == 3
        assert doc["creation_quarter_label"] == "2014-Q3"
        assert doc["fiscal_quarter"] == 1
        assert doc["fiscal_quarter_label"] == "2014-2015 Q1"

    def test_calendar_and_fiscal_quarters_differ(self) -> None:
        """January is calendar Q1 but fiscal Q3 — the reason FR-014b exists."""
        doc = {"creation_date": datetime(2015, 1, 15), "fiscal_year": "2014-2015"}
        derive_periods(doc)

        assert doc["creation_quarter_label"] == "2015-Q1"
        assert doc["fiscal_quarter_label"] == "2014-2015 Q3"

    def test_null_date_yields_null_periods(self) -> None:
        doc: dict[str, object] = {"creation_date": None, "fiscal_year": None}
        derive_periods(doc)

        for field in ("creation_year", "creation_quarter_label", "fiscal_quarter_label"):
            assert doc[field] is None

    def test_falls_back_to_derived_fiscal_year_when_source_blank(self) -> None:
        doc = {"creation_date": datetime(2014, 8, 27), "fiscal_year": None}
        derive_periods(doc)
        assert doc["fiscal_quarter_label"] == "2014-2015 Q1"


class TestTransformRow:
    def test_realistic_row(self) -> None:
        """The first data row of the source file."""
        row = {
            "Creation Date": "08/27/2013",
            "Purchase Date": "",
            "Fiscal Year": "2013-2014",
            "LPA Number": "7-12-70-26",
            "Purchase Order Number": "REQ0011118",
            "Acquisition Type": "IT Goods",
            "Acquisition Method": "WSCA/Coop",
            "Department Name": "Consumer Affairs, Department of",
            "Supplier Code": "1740272",
            "Supplier Name": "Pitney Bowes",
            "CalCard": "NO",
            "Item Name": "USB",
            "Item Description": "USB",
            "Quantity": "1",
            "Unit Price": "$1.00",
            "Total Price": "$1.00",
        }
        doc = transform_row(row)

        assert doc["creation_date"] == datetime(2013, 8, 27)
        assert doc["purchase_date"] is None
        assert doc["total_price"] == pytest.approx(1.00)
        assert doc["quantity"] == pytest.approx(1.0)
        assert doc["calcard"] is False
        assert doc["department_name"] == "Consumer Affairs, Department of"
        assert doc["department_name_normalized"] == "consumer affairs, department of"
        assert doc["creation_quarter_label"] == "2013-Q3"
        assert doc["fiscal_quarter_label"] == "2013-2014 Q1"

    def test_row_missing_every_optional_field_still_loads(self) -> None:
        """FR-030: a shrinking dataset is worse than a null."""
        doc = transform_row({"Purchase Order Number": "PO-1"})

        assert doc["purchase_order_number"] == "PO-1"
        assert doc["creation_date"] is None
        assert doc["total_price"] is None
        assert doc["department_name"] is None
        assert doc["calcard"] is False

    def test_supplier_code_stays_a_string(self) -> None:
        """Leading zeros are meaningful; an int would eat them."""
        doc = transform_row({"Supplier Code": "0001740"})
        assert doc["supplier_code"] == "0001740"

    def test_source_misspelling_is_preserved(self) -> None:
        """`Expert Witneses` is in the data. Correcting it would match nothing."""
        doc = transform_row({"Sub-Acquisition Type": "Expert Witneses"})
        assert doc["sub_acquisition_type"] == "Expert Witneses"

    def test_negative_total_survives_the_full_path(self) -> None:
        doc = transform_row({"Total Price": "($1,234.56)", "Quantity": "-1"})
        assert doc["total_price"] == pytest.approx(-1234.56)
        assert doc["quantity"] == pytest.approx(-1.0)


class TestParseNumber:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("1", 1.0), ("1,000", 1000.0), ("2.5", 2.5), ("-3", -3.0)]
    )
    def test_quantities(self, raw: str, expected: float) -> None:
        assert parse_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", None, "abc"])
    def test_unparseable(self, raw: str | None) -> None:
        assert parse_number(raw) is None
