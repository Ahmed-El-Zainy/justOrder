# Evaluation Report

**Model**: `deepseek/deepseek-v4-flash` | **Tag**: `deepseek` | **Cases**: 26

Expected values are computed in pandas directly from the source CSV by
`evals/ground_truth.py`, which imports nothing from `backend/` and never queries
MongoDB. A bug shared by the transform and a generated pipeline therefore shows up
as a disagreement rather than being reproduced identically on both sides.

## Headline

| Metric | Result | Target |
|---|---|---|
| Overall accuracy | **84.6%** (22/26) | ≥85% (SC-001) |
| Follow-up accuracy | **75.0%** (3/4) | ≥80% (SC-003) |
| Median latency per turn | 17.5s | — |
| p95 latency per turn | 30.0s | <15s (SC-004) |
| Slowest single case | 55.5s | ≤30s ceiling |

## By category

| Category | Passed | Total | Accuracy |
|---|---|---|---|
| aggregation | 3 | 5 | 60% |
| ambiguity | 1 | 1 | 100% |
| combined_filter | 1 | 1 | 100% |
| counting | 4 | 4 | 100% |
| coverage | 2 | 2 | 100% |
| credits | 1 | 1 | 100% |
| empty_result | 1 | 1 | 100% |
| followup | 3 | 4 | 75% |
| out_of_scope | 2 | 2 | 100% |
| ranking | 3 | 4 | 75% |
| time_series | 1 | 1 | 100% |

## Failures

### `top_item_count` (ranking)

- **Question**: How many times was the most frequently ordered item ordered?
- **Expected**: `3839`
- **Actual**: ERROR: That question took longer than 30 seconds, so I stopped. A narrower question usually answers faster.
- **Why it failed**: exceeded the per-question deadline (expected 3,839.00, answer had [30.0])

### `top_dept_it_services` (aggregation)

- **Question**: Which departments spent the most on IT services?
- **Expected**: `['Social Services']`
- **Actual**: ERROR: That question took longer than 30 seconds, so I stopped. A narrower question usually answers faster.
- **Why it failed**: exceeded the per-question deadline (missing ['Social Services'])

### `top_dept_it_services_amount` (aggregation)

- **Question**: How much did the top-spending department spend on IT services?
- **Expected**: `1016701639.99`
- **Actual**: ERROR: That question took longer than 30 seconds, so I stopped. A narrower question usually answers faster.
- **Why it failed**: exceeded the per-question deadline (expected 1,016,701,639.99, answer had [30.0])

### `followup_breakdown` (followup)

- **Question**: Break that down by department
- **Expected**: `['Health Care Services']`
- **Actual**: ERROR: That question took longer than 30 seconds, so I stopped. A narrower question usually answers faster.
- **Why it failed**: exceeded the per-question deadline (missing ['Health Care Services'])


## All cases

| Case | Category | Result | Rows | Attempts | Time |
|---|---|---|---|---|---|
| `orders_q3_2014` | counting | pass | 1 | 1 | 10.0s |
| `orders_2013` | counting | pass | 1 | 1 | 13.5s |
| `orders_calcard` | counting | pass | 1 | 1 | 11.6s |
| `line_items_total` | counting | pass | 1 | 1 | 19.8s |
| `highest_quarter` | ranking | pass | 1 | 1 | 15.1s |
| `highest_quarter_amount` | ranking | pass | 1 | 1 | 27.9s |
| `top_items` | ranking | pass | 10 | 1 | 16.2s |
| `top_item_count` | ranking | **FAIL** | 0 | 1 | 30.0s |
| `top_dept_it_services` | aggregation | **FAIL** | 10 | 1 | 30.0s |
| `top_dept_it_services_amount` | aggregation | **FAIL** | 1 | 1 | 30.0s |
| `top_supplier` | aggregation | pass | 1 | 1 | 20.8s |
| `top_department_overall` | aggregation | pass | 1 | 1 | 19.3s |
| `spend_by_acq_type` | aggregation | pass | 1 | 1 | 12.5s |
| `it_goods_2013_suppliers` | combined_filter | pass | 10 | 1 | 15.8s |
| `monthly_2014` | time_series | pass | 12 | 1 | 23.7s |
| `credits_are_net` | credits | pass | 1 | 1 | 7.5s |
| `period_not_covered` | coverage | pass | 0 | 1 | 7.3s |
| `period_covered_edge` | coverage | pass | 0 | 1 | 1.3s |
| `unknown_supplier` | empty_result | pass | 0 | 1 | 18.8s |
| `ambiguous_department` | ambiguity | pass | 0 | 1 | 1.3s |
| `off_topic` | out_of_scope | pass | 0 | 1 | 2.4s |
| `off_topic_adjacent` | out_of_scope | pass | 0 | 1 | 2.8s |
| `followup_next_quarter` | followup | pass | 1 | 1 | 38.5s |
| `followup_breakdown` | followup | **FAIL** | 90 | 1 | 43.2s |
| `followup_topic_change` | followup | pass | 10 | 1 | 55.5s |
| `followup_ordinal` | followup | pass | 3 | 1 | 44.8s |
