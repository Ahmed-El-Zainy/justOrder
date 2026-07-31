"""Entity grounding.

Covers the cases that separate a correct answer from a confident wrong one:
inverted word order, the misspelling that is genuinely in the source data, and
the ambiguous match that must produce a question rather than a guess.
"""

from __future__ import annotations

import pytest

from app.agent.vocabulary import MatchKind, match_value

THRESHOLD = 90
FLOOR = 75

# Mirrors real values from the loaded dataset, including the two departments
# that genuinely collide on "Corrections" and the two that collide on
# "Transportation" — the ambiguity FR-014a exists to handle is in the data, not
# invented for the test.
DEPARTMENTS = {
    "Consumer Affairs, Department of": 5_000,
    "Corrections and Rehabilitation, Department of": 40_000,
    "Board of State and Community Corrections": 900,
    "Correctional Health Care Services": 3_000,
    "Transportation, Department of": 30_000,
    "Transportation Commission, California": 600,
    "Water Resources, Department of": 12_000,
    "Motor Vehicles, Department of": 8_000,
}

ACQUISITION_TYPES = {
    "NON-IT Goods": 215_083,
    "NON-IT Services": 68_372,
    "IT Goods": 50_900,
    "IT Services": 11_516,
    "IT Telecommunications": 147,
}

SUB_TYPES = {
    "Expert Witneses": 400,  # sic — the source data misspells this
    "Consulting Services": 2_000,
    "Interagency Agreements": 1_500,
}


def resolve(query: str, candidates: dict[str, int]) -> object:
    return match_value(query, candidates, threshold=THRESHOLD, floor=FLOOR, field="test")


class TestExactMatch:
    def test_exact_string(self) -> None:
        result = resolve("IT Goods", ACQUISITION_TYPES)
        assert result.kind is MatchKind.EXACT
        assert result.value == "IT Goods"

    def test_case_insensitive(self) -> None:
        result = resolve("it goods", ACQUISITION_TYPES)
        assert result.kind is MatchKind.EXACT
        assert result.value == "IT Goods"

    def test_uppercase_non_it_matched_as_stored(self) -> None:
        """The data stores 'NON-IT Goods'; a user types 'Non-IT Goods'."""
        result = resolve("Non-IT Goods", ACQUISITION_TYPES)
        assert result.value == "NON-IT Goods"

    def test_surrounding_whitespace(self) -> None:
        assert resolve("  IT Services  ", ACQUISITION_TYPES).value == "IT Services"


class TestInvertedWordOrder:
    def test_department_of_x_finds_x_department_of(self) -> None:
        """The single most common real-world phrasing mismatch."""
        result = resolve("Department of Consumer Affairs", DEPARTMENTS)
        assert result.value == "Consumer Affairs, Department of"

    def test_transportation(self) -> None:
        result = resolve("Department of Transportation", DEPARTMENTS)
        assert result.value == "Transportation, Department of"


class TestSourceMisspelling:
    def test_correctly_spelled_query_finds_misspelled_record(self) -> None:
        """`Expert Witneses` is in the data. Correcting it would match nothing."""
        result = resolve("Expert Witnesses", SUB_TYPES)
        assert result.value == "Expert Witneses"


class TestAmbiguity:
    def test_several_close_matches_ask_rather_than_guess(self) -> None:
        """FR-014a: 'Corrections' plausibly means either of two departments."""
        result = resolve("Corrections", DEPARTMENTS)
        assert result.kind is MatchKind.AMBIGUOUS
        assert result.value is None
        assert len(result.candidates) >= 2

    def test_ambiguous_candidates_are_ordered_by_score_then_frequency(self) -> None:
        result = resolve("Corrections", DEPARTMENTS)
        scores = [c.score for c in result.candidates]
        assert scores == sorted(scores, reverse=True)


class TestNoMatch:
    def test_unrelated_name_matches_nothing(self) -> None:
        result = resolve("Ministry of Magic", DEPARTMENTS)
        assert result.kind is MatchKind.NONE
        assert result.value is None

    def test_empty_query(self) -> None:
        assert resolve("", DEPARTMENTS).kind is MatchKind.NONE

    def test_empty_vocabulary(self) -> None:
        assert resolve("IT Goods", {}).kind is MatchKind.NONE


class TestThresholdBehaviour:
    def test_below_floor_is_no_match(self) -> None:
        result = match_value("zzzzzzz", DEPARTMENTS, threshold=90, floor=75, field="d")
        assert result.kind is MatchKind.NONE

    def test_raising_the_floor_suppresses_weak_matches(self) -> None:
        """A partial-word query matches at the default floor but not a strict one."""
        lenient = match_value("Motor Vehicl", DEPARTMENTS, threshold=90, floor=75, field="d")
        assert lenient.kind is not MatchKind.NONE

        strict = match_value("Motor Vehicl", DEPARTMENTS, threshold=99, floor=98, field="d")
        assert strict.kind is MatchKind.NONE

    def test_single_strong_match_substitutes_silently(self) -> None:
        result = match_value(
            "Water Resources, Department of", DEPARTMENTS, threshold=90, floor=75, field="d"
        )
        assert result.kind is MatchKind.EXACT
        assert result.value == "Water Resources, Department of"


class TestCandidateMetadata:
    def test_count_is_carried_through(self) -> None:
        result = resolve("IT Goods", ACQUISITION_TYPES)
        assert result.candidates[0].count == 50_900

    def test_score_is_present(self) -> None:
        result = resolve("Department of Consumer Affairs", DEPARTMENTS)
        assert 0 < result.candidates[0].score <= 100

    @pytest.mark.parametrize("query", ["IT Goods", "Corrections", "nonsense"])
    def test_field_is_preserved(self, query: str) -> None:
        assert resolve(query, DEPARTMENTS).field == "test"
