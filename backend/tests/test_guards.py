"""Pipeline validator — constitution Principle II merge gate.

This is the only thing standing between a model-generated pipeline and the
database driver. Principle II states it must not be bypassable by prompt
content, so every assertion here operates on the validator alone, with no model
in the loop.

An allow-list, not a deny-list: a stage MongoDB adds in a future version is
refused by default rather than silently permitted.
"""

from __future__ import annotations

import pytest

from app.agent.guards import (
    FORBIDDEN_STAGES,
    PipelineRejected,
    validate_pipeline,
)

COLLECTION = "purchase_orders"


def _valid() -> list[dict]:
    return [{"$match": {"acquisition_type": "IT Goods"}}, {"$count": "n"}]


class TestForbiddenStages:
    @pytest.mark.parametrize(
        "stage",
        [
            {"$out": "stolen_copy"},
            {"$merge": {"into": "stolen_copy"}},
            {"$where": "function() { return true; }"},
            {"$function": {"body": "function(){}", "args": [], "lang": "js"}},
            {"$accumulator": {"init": "function(){}", "lang": "js"}},
            {"$graphLookup": {"from": "purchase_orders", "startWith": "$x"}},
            {"$lookup": {"from": "field_vocabulary", "localField": "a"}},
            {"$unionWith": {"coll": "field_vocabulary"}},
            {"$currentOp": {}},
            {"$listSessions": {}},
            {"$planCacheStats": {}},
            {"$collStats": {}},
            {"$indexStats": {}},
        ],
    )
    def test_each_forbidden_stage_is_rejected(self, stage: dict) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([stage], COLLECTION)

    def test_forbidden_stage_hidden_among_valid_ones(self) -> None:
        pipeline = [
            {"$match": {"acquisition_type": "IT Goods"}},
            {"$group": {"_id": "$department_name", "total": {"$sum": "$total_price"}}},
            {"$out": "exfiltrated"},
        ]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_forbidden_stage_nested_in_facet_is_still_rejected(self) -> None:
        """A deny-list that only checks top level would wave this through."""
        pipeline = [
            {
                "$facet": {
                    "safe": [{"$count": "n"}],
                    "sneaky": [{"$out": "exfiltrated"}],
                }
            }
        ]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_deeply_nested_facet(self) -> None:
        pipeline = [
            {
                "$facet": {
                    "outer": [
                        {"$match": {"x": 1}},
                        {"$facet": {"inner": [{"$where": "true"}]}},
                    ]
                }
            }
        ]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_unknown_stage_is_rejected_by_default(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([{"$someFutureStage": {}}], COLLECTION)

    def test_forbidden_list_covers_the_documented_set(self) -> None:
        for stage in ("$out", "$merge", "$where", "$function", "$accumulator", "$graphLookup"):
            assert stage in FORBIDDEN_STAGES


class TestExpressionLevelCodeExecution:
    def test_expr_containing_function_is_rejected(self) -> None:
        pipeline = [{"$match": {"$expr": {"$function": {"body": "function(){}", "args": []}}}}]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_where_nested_inside_match_is_rejected(self) -> None:
        pipeline = [{"$match": {"$where": "this.total_price > 0"}}]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_function_deep_inside_group_is_rejected(self) -> None:
        pipeline = [
            {
                "$group": {
                    "_id": "$department_name",
                    "weird": {"$accumulator": {"init": "function(){}", "lang": "js"}},
                }
            }
        ]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)


class TestCollectionPinning:
    def test_other_collection_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline(_valid(), "field_vocabulary")

    def test_target_collection_is_accepted(self) -> None:
        validate_pipeline(_valid(), COLLECTION)


class TestRowBound:
    def test_limit_is_injected_when_absent(self) -> None:
        """FR-013: an unbounded $match would stream 346,018 documents."""
        result = validate_pipeline([{"$match": {"acquisition_type": "IT Goods"}}], COLLECTION)
        assert result[-1] == {"$limit": 200}

    def test_existing_smaller_limit_is_left_alone(self) -> None:
        pipeline = [{"$match": {}}, {"$limit": 10}]
        result = validate_pipeline(pipeline, COLLECTION)
        assert result[-1] == {"$limit": 10}

    def test_oversized_limit_is_clamped(self) -> None:
        result = validate_pipeline([{"$match": {}}, {"$limit": 100_000}], COLLECTION)
        assert result[-1] == {"$limit": 200}

    def test_count_needs_no_limit(self) -> None:
        result = validate_pipeline([{"$match": {}}, {"$count": "n"}], COLLECTION)
        assert result[-1] == {"$count": "n"}

    def test_group_still_gets_a_bound(self) -> None:
        """A $group on a high-cardinality field can emit tens of thousands of rows."""
        pipeline = [{"$group": {"_id": "$supplier_name", "total": {"$sum": "$total_price"}}}]
        result = validate_pipeline(pipeline, COLLECTION)
        assert {"$limit": 200} in result


class TestFieldReferences:
    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([{"$match": {"nonexistent_field": "x"}}], COLLECTION)

    def test_known_fields_are_accepted(self) -> None:
        pipeline = [
            {"$match": {"department_name": "Corrections and Rehabilitation, Department of"}},
            {"$group": {"_id": "$creation_quarter_label", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 5},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_operators_are_not_mistaken_for_fields(self) -> None:
        pipeline = [
            {"$match": {"total_price": {"$gte": 1000, "$lte": 5000}}},
            {"$count": "n"},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_names_computed_by_an_earlier_stage_are_available_later(self) -> None:
        """`spend` is not a column — $group invented it. Rejecting it here would
        refuse the ordinary group-then-sort-then-project shape."""
        pipeline = [
            {"$group": {"_id": "$department_name", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$project": {"department": "$_id", "spend": 1}},
            {"$limit": 5},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_group_output_replaces_the_original_schema(self) -> None:
        """After $group only _id and the accumulators exist."""
        pipeline = [
            {"$group": {"_id": "$creation_quarter_label", "spend": {"$sum": "$total_price"}}},
            {"$match": {"department_name": "x"}},
        ]
        with pytest.raises(PipelineRejected):
            validate_pipeline(pipeline, COLLECTION)

    def test_addfields_extends_rather_than_replaces(self) -> None:
        pipeline = [
            {"$addFields": {"doubled": {"$multiply": ["$total_price", 2]}}},
            {"$match": {"department_name": "x", "doubled": {"$gt": 10}}},
            {"$count": "n"},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_count_output_is_referenceable(self) -> None:
        pipeline = [
            {"$group": {"_id": "$purchase_order_number"}},
            {"$count": "orders"},
            {"$sort": {"orders": -1}},
        ]
        validate_pipeline(pipeline, COLLECTION)


class TestStructure:
    def test_empty_pipeline_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([], COLLECTION)

    def test_non_list_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline({"$match": {}}, COLLECTION)  # type: ignore[arg-type]

    def test_stage_with_two_keys_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([{"$match": {}, "$count": "n"}], COLLECTION)

    def test_absurdly_long_pipeline_is_rejected(self) -> None:
        with pytest.raises(PipelineRejected):
            validate_pipeline([{"$match": {}}] * 100, COLLECTION)


class TestRealisticPipelines:
    """The four named question types must pass unmodified except for bounding."""

    def test_distinct_order_count(self) -> None:
        pipeline = [
            {"$match": {"creation_quarter_label": "2014-Q3"}},
            {"$group": {"_id": "$purchase_order_number"}},
            {"$count": "orders"},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_highest_spending_quarter(self) -> None:
        pipeline = [
            {"$group": {"_id": "$creation_quarter_label", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 12},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_most_frequent_items(self) -> None:
        pipeline = [
            {"$group": {"_id": "$item_name_normalized", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 10},
        ]
        validate_pipeline(pipeline, COLLECTION)

    def test_department_spend_with_category_filter(self) -> None:
        pipeline = [
            {"$match": {"acquisition_type": "IT Services"}},
            {"$group": {"_id": "$department_name", "spend": {"$sum": "$total_price"}}},
            {"$sort": {"spend": -1}},
            {"$limit": 10},
        ]
        validate_pipeline(pipeline, COLLECTION)


class TestNoNetworkAccess:
    def test_guards_module_imports_nothing_networked(self) -> None:
        """Invariant 6: the validator must be decidable offline."""
        import app.agent.guards as guards

        source = guards.__file__
        with open(source) as handle:
            text = handle.read()

        for forbidden_import in ("import httpx", "import requests", "from openai", "import openai"):
            assert forbidden_import not in text
