# Model Comparison

Same code, same 26-case golden set, same 30-second per-question deadline.

A **timeout** means the model was still working when the deadline cut it off —
the system behaved correctly, and it says nothing about whether the model would
have been right. A **wrong answer** is the one that matters for trust.

| Case | Category | `deepseek/deepseek-v4-flash` | `google/gemma-4-26b-a4b-it:free` |
|---|---|---|---|
| `orders_q3_2014` | counting | pass (10s) | pass (21s) |
| `orders_2013` | counting | pass (13s) | pass (26s) |
| `orders_calcard` | counting | pass (12s) | **FAIL** |
| `line_items_total` | counting | pass (20s) | pass (21s) |
| `highest_quarter` | ranking | pass (15s) | pass (19s) |
| `highest_quarter_amount` | ranking | pass (28s) | pass (28s) |
| `top_items` | ranking | pass (16s) | pass (10s) |
| `top_item_count` | ranking | **timeout** | pass (24s) |
| `top_dept_it_services` | aggregation | **timeout** | pass (22s) |
| `top_dept_it_services_amount` | aggregation | **timeout** | pass (15s) |
| `top_supplier` | aggregation | pass (21s) | pass (29s) |
| `top_department_overall` | aggregation | pass (19s) | pass (26s) |
| `spend_by_acq_type` | aggregation | pass (12s) | pass (27s) |
| `it_goods_2013_suppliers` | combined_filter | pass (16s) | pass (28s) |
| `monthly_2014` | time_series | pass (24s) | **timeout** |
| `credits_are_net` | credits | pass (8s) | pass (20s) |
| `period_not_covered` | coverage | pass (7s) | pass (7s) |
| `period_covered_edge` | coverage | pass (1s) | pass (7s) |
| `unknown_supplier` | empty_result | pass (19s) | pass (21s) |
| `ambiguous_department` | ambiguity | pass (1s) | pass (10s) |
| `off_topic` | out_of_scope | pass (2s) | pass (2s) |
| `off_topic_adjacent` | out_of_scope | pass (3s) | pass (13s) |
| `followup_next_quarter` | followup | pass (38s) | **FAIL** |
| `followup_breakdown` | followup | **timeout** | pass (40s) |
| `followup_topic_change` | followup | pass (55s) | **timeout** |
| `followup_ordinal` | followup | pass (45s) | **FAIL** |

## Totals

| Model | Overall | Wrong answers | Timeouts | Accuracy excl. timeouts | Follow-ups | Median/turn |
|---|---|---|---|---|---|---|
| `deepseek/deepseek-v4-flash` | **22/26** (84.6%) | 0 | 4 | 22/22 (100.0%) | 3/4 | 17.5s |
| `google/gemma-4-26b-a4b-it:free` | **21/26** (80.8%) | 3 | 2 | 21/24 (87.5%) | 1/4 | 21.1s |
