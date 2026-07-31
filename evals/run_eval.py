"""Score the assistant against the golden set.

Expected values come from `ground_truth.json`, computed in pandas straight from
the source CSV. This module never queries MongoDB and never imports the agent's
query path, so a bug shared by both sides cannot mark itself correct.

Results accumulate in `results.json`, so the set can be run in batches — which
the free tier requires: each question costs about three model calls, and 26
cases need roughly 90 against a 50/day allowance.

A case whose model call failed is left UNRECORDED rather than marked failed, so
the next batch retries it. Recording an outage as a wrong answer would quietly
turn a quota problem into a permanent accuracy figure.

Run:
    python -m evals.run_eval --status              # what is done, what is left
    python -m evals.run_eval --category counting   # one batch
    python -m evals.run_eval                       # everything still outstanding
    python -m evals.run_eval --rerun               # ignore stored results
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "golden_set.yaml"
TRUTH_PATH = HERE / "ground_truth.json"
REPORT_PATH = HERE / "report.md"
RESULTS_DIR = HERE / "results"


def results_path(model: str) -> Path:
    """One store per model, so runs can be compared rather than overwritten."""
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return RESULTS_DIR / f"{slug}.json"
API = "http://localhost:8000/api/chat"

# Any run of digits with optional separators and decimals.
_NUMBER = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")

REFUSAL_MARKERS = (
    "no matching records",
    "outside the data",
    "not covered",
    "i can only answer",
    "did you mean",
    "matches several",
    "couldn't build",
    "couldn't reach",
    "not sure what you're asking",
)


# The provider was never reached: quota, credit or connectivity. These say
# nothing about accuracy, so the case is left unrecorded and the batch stops.
OUTAGE_MARKERS = (
    "couldn't reach the language model",
    "rate limit",
    "error code: 429",
    "error code: 402",
)

# The model was reached but did not finish inside the per-question deadline.
# That IS a case outcome — the assistant correctly gave up — so it is recorded
# as a failure, and the batch carries on to the next case.
TIMEOUT_MARKER = "took longer than"


def is_outage(answer: str) -> bool:
    """Infrastructure failure: unrecordable, and a reason to stop the batch."""
    lowered = answer.lower()
    return not answer.strip() or any(marker in lowered for marker in OUTAGE_MARKERS)


def is_timeout(answer: str) -> bool:
    return TIMEOUT_MARKER in answer.lower()


@dataclass
class Result:
    case_id: str
    category: str
    question: str
    passed: bool
    expected: Any
    actual: str
    reason: str = ""
    elapsed_s: float = 0.0
    turns: int = 1
    pipeline: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    attempts: int = 1


def resolve_path(truth: dict[str, Any], path: str) -> Any:
    """Look up `a.b.0.c` in the ground-truth document."""
    node: Any = truth
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for match in _NUMBER.finditer(text):
        try:
            out.append(float(match.group().replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return out


def ask(question: str, session_id: str | None, timeout: int = 150) -> dict[str, Any]:
    """One turn against the live API, collecting the SSE stream."""
    body = json.dumps({"question": question, "session_id": session_id}).encode()
    request = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})

    collected: dict[str, Any] = {
        "answer": "",
        "pipeline": [],
        "row_count": 0,
        "attempts": 1,
        "session_id": session_id,
    }

    event = None
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                payload = json.loads(line.split(":", 1)[1].strip())
                if event == "pipeline":
                    collected["pipeline"] = payload.get("pipeline", [])
                    collected["attempts"] = payload.get("attempt", 1)
                elif event == "rows":
                    collected["row_count"] = payload.get("row_count", 0)
                elif event == "clarification":
                    collected["answer"] = payload.get("question", "")
                elif event == "done":
                    collected["answer"] = payload.get("answer", "")
                    collected["session_id"] = payload.get("session_id")
                elif event == "error":
                    collected["answer"] = f"ERROR: {payload.get('message', '')}"
    return collected


def check(case: dict[str, Any], truth: dict[str, Any], answer: str) -> tuple[bool, str]:
    match_type = case.get("match", "contains")
    lowered = answer.lower()

    if match_type == "numeric":
        expected = resolve_path(truth, case["expected_path"])
        tolerance = float(case.get("tolerance", 0))
        found = numbers_in(answer)
        if not found:
            return False, "no figure in the answer"
        for value in found:
            if abs(value - float(expected)) <= max(tolerance, abs(float(expected)) * 1e-6):
                return True, ""
        return False, f"expected {expected:,.2f}, answer had {found[:4]}"

    if match_type == "contains":
        missing = [
            literal
            for literal in case.get("expected_literal", [])
            if literal.lower() not in lowered
        ]
        return (not missing), (f"missing {missing}" if missing else "")

    if match_type == "absent":
        present = [
            literal for literal in case.get("expected_literal", []) if literal.lower() in lowered
        ]
        return (not present), (f"should not mention {present}" if present else "")

    if match_type == "refusal":
        refused = any(marker in lowered for marker in REFUSAL_MARKERS)
        if not refused:
            return False, "did not decline, ask, or report emptiness"
        missing = [
            literal
            for literal in case.get("expected_literal", [])
            if literal.lower() not in lowered
        ]
        return (not missing), (f"missing {missing}" if missing else "")

    return False, f"unknown match type {match_type!r}"


def run_case(case: dict[str, Any], truth: dict[str, Any]) -> Result:
    turns = case.get("turns") or [case["question"]]
    started = time.perf_counter()
    session_id: str | None = None
    reply: dict[str, Any] = {}

    try:
        for turn in turns:
            reply = ask(turn, session_id)
            session_id = reply.get("session_id")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Result(
            case_id=case["id"],
            category=case["category"],
            question=turns[-1],
            passed=False,
            expected=case.get("expected_path") or case.get("expected_literal"),
            actual=f"request failed: {exc}",
            reason="transport error",
            elapsed_s=time.perf_counter() - started,
            turns=len(turns),
        )

    elapsed = time.perf_counter() - started
    answer = reply.get("answer", "")
    passed, reason = check(case, truth, answer)

    expected: Any = case.get("expected_literal")
    if case.get("expected_path"):
        try:
            expected = resolve_path(truth, case["expected_path"])
        except (KeyError, IndexError, ValueError):
            expected = f"<missing: {case['expected_path']}>"

    return Result(
        case_id=case["id"],
        category=case["category"],
        question=turns[-1],
        passed=passed,
        expected=expected,
        actual=answer,
        reason=reason,
        elapsed_s=elapsed,
        pipeline=reply.get("pipeline", []),
        row_count=reply.get("row_count", 0),
        attempts=reply.get("attempts", 1),
        turns=len(turns),
    )


def load_results(model: str) -> dict[str, dict[str, Any]]:
    path = results_path(model)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_results(model: str, store: dict[str, dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_path(model).write_text(json.dumps(store, indent=2, sort_keys=True))


def write_comparison(cases: list[dict[str, Any]]) -> None:
    """Side-by-side table across every model that has recorded results."""
    stores: dict[str, dict[str, Any]] = {}
    for path in sorted(RESULTS_DIR.glob("*.json")):
        store = json.loads(path.read_text())
        if store:
            model = next(iter(store.values())).get("model", path.stem)
            stores[model] = store
    if not stores:
        print("[eval] no recorded results to compare")
        return

    models = sorted(stores)
    lines = [
        "# Model Comparison",
        "",
        "Same code, same 26-case golden set, same 30-second per-question deadline.",
        "",
        "A **timeout** means the model was still working when the deadline cut it off —",
        "the system behaved correctly, and it says nothing about whether the model would",
        "have been right. A **wrong answer** is the one that matters for trust.",
        "",
    ]

    header = "| Case | Category | " + " | ".join(f"`{m}`" for m in models) + " |"
    lines += [header, "|---|---|" + "---|" * len(models)]

    for case in cases:
        cells = []
        for model in models:
            record = stores[model].get(case["id"])
            if record is None:
                cells.append("-")
            elif record["passed"]:
                cells.append(f"pass ({record['elapsed_s']:.0f}s)")
            else:
                why = "timeout" if "deadline" in record.get("reason", "") else "FAIL"
                cells.append(f"**{why}**")
        lines.append(f"| `{case['id']}` | {case['category']} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Totals",
        "",
        (
            "| Model | Overall | Wrong answers | Timeouts | "
            "Accuracy excl. timeouts | Follow-ups | Median/turn |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for model in models:
        store = stores[model]
        total = len(store)
        passed = sum(1 for r in store.values() if r["passed"])
        fu = [r for r in store.values() if r["category"] == "followup"]
        fu_passed = sum(1 for r in fu if r["passed"])
        lat = sorted(r["elapsed_s"] / max(r.get("turns", 1), 1) for r in store.values())
        median = statistics.median(lat) if lat else 0.0
        pct = passed / total * 100 if total else 0
        timeouts = sum(
            1 for r in store.values() if not r["passed"] and "deadline" in r.get("reason", "")
        )
        wrong = total - passed - timeouts
        judged = total - timeouts
        excl = passed / judged * 100 if judged else 0
        lines.append(
            f"| `{model}` | **{passed}/{total}** ({pct:.1f}%) | {wrong} | {timeouts} | "
            f"{passed}/{judged} ({excl:.1f}%) | {fu_passed}/{len(fu)} | {median:.1f}s |"
        )

    (HERE / "comparison.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[-(len(models) + 4):]))
    print(f"\n[eval] wrote {(HERE / 'comparison.md').name}")


def to_record(result: Result, model: str) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "category": result.category,
        "question": result.question,
        "passed": result.passed,
        "expected": result.expected,
        "actual": result.actual,
        "reason": result.reason,
        "elapsed_s": round(result.elapsed_s, 2),
        "turns": result.turns,
        "row_count": result.row_count,
        "attempts": result.attempts,
        "model": model,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def from_record(record: dict[str, Any]) -> Result:
    return Result(
        case_id=record["case_id"],
        category=record["category"],
        question=record.get("question", ""),
        passed=record["passed"],
        expected=record.get("expected"),
        actual=record.get("actual", ""),
        reason=record.get("reason", ""),
        elapsed_s=record.get("elapsed_s", 0.0),
        turns=record.get("turns", 1),
        row_count=record.get("row_count", 0),
        attempts=record.get("attempts", 1),
    )


def write_report(results: list[Result], tag: str, model: str) -> None:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    overall = (passed / total * 100) if total else 0.0

    by_category: dict[str, list[Result]] = {}
    for result in results:
        by_category.setdefault(result.category, []).append(result)

    latencies = sorted(result.elapsed_s / max(result.turns, 1) for result in results)
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = (
        latencies[int(len(latencies) * 0.95) - 1] if len(latencies) >= 2 else (latencies or [0])[0]
    )

    followups = by_category.get("followup", [])
    followup_rate = (
        sum(1 for result in followups if result.passed) / len(followups) * 100 if followups else 0.0
    )

    lines: list[str] = [
        "# Evaluation Report",
        "",
        f"**Model**: `{model}` | **Tag**: `{tag}` | **Cases**: {total}",
        "",
        "Expected values are computed in pandas directly from the source CSV by",
        "`evals/ground_truth.py`, which imports nothing from `backend/` and never queries",
        "MongoDB. A bug shared by the transform and a generated pipeline therefore shows up",
        "as a disagreement rather than being reproduced identically on both sides.",
        "",
        "## Headline",
        "",
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Overall accuracy | **{overall:.1f}%** ({passed}/{total}) | ≥85% (SC-001) |",
        (
            f"| Follow-up accuracy | **{followup_rate:.1f}%** "
            f"({sum(1 for r in followups if r.passed)}/{len(followups)}) | ≥80% (SC-003) |"
        ),
        f"| Median latency per turn | {p50:.1f}s | — |",
        f"| p95 latency per turn | {p95:.1f}s | <15s (SC-004) |",
        (
            f"| Slowest single case | "
            f"{max((r.elapsed_s for r in results), default=0):.1f}s | ≤30s ceiling |"
        ),
        "",
        "## By category",
        "",
        "| Category | Passed | Total | Accuracy |",
        "|---|---|---|---|",
    ]

    for category in sorted(by_category):
        cases = by_category[category]
        count = sum(1 for case in cases if case.passed)
        lines.append(f"| {category} | {count} | {len(cases)} | {count / len(cases) * 100:.0f}% |")

    failures = [result for result in results if not result.passed]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    else:
        for result in failures:
            lines += [
                f"### `{result.case_id}` ({result.category})",
                "",
                f"- **Question**: {result.question}",
                f"- **Expected**: `{result.expected}`",
                f"- **Actual**: {result.actual[:400]}",
                f"- **Why it failed**: {result.reason}",
                "",
            ]

    lines += [
        "",
        "## All cases",
        "",
        "| Case | Category | Result | Rows | Attempts | Time |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        mark = "pass" if result.passed else "**FAIL**"
        lines.append(
            f"| `{result.case_id}` | {result.category} | {mark} | {result.row_count} | "
            f"{result.attempts} | {result.elapsed_s:.1f}s |"
        )

    REPORT_PATH.write_text("\n".join(lines) + "\n")


def resolve_model() -> str:
    try:
        payload = json.loads(
            urllib.request.urlopen("http://localhost:8000/health", timeout=60).read()
        )
        return str(payload["llm"]["model"])
    except Exception:
        raise SystemExit("[eval] the backend is not reachable on localhost:8000") from None


def print_status(cases: list[dict[str, Any]], store: dict[str, dict[str, Any]]) -> None:
    by_category: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_category.setdefault(case["category"], []).append(case)

    print(f"{'category':<16} {'done':>5} {'left':>5} {'passed':>7}")
    print("-" * 38)
    total_done = total_left = total_passed = 0
    for category in sorted(by_category):
        group = by_category[category]
        done = [c for c in group if c["id"] in store]
        passed = [c for c in done if store[c["id"]]["passed"]]
        left = len(group) - len(done)
        total_done += len(done)
        total_left += left
        total_passed += len(passed)
        flag = "" if left == 0 else "  <- outstanding"
        print(f"{category:<16} {len(done):>5} {left:>5} {len(passed):>7}{flag}")
    print("-" * 38)
    print(f"{'TOTAL':<16} {total_done:>5} {total_left:>5} {total_passed:>7}")
    if total_left:
        print("\nRun the next batch:  python -m evals.run_eval --category <name>")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the assistant against the golden set")
    parser.add_argument("--tag", default="default", help="label for this run")
    parser.add_argument("--limit", type=int, help="run at most N cases this batch")
    parser.add_argument("--category", help="run only one category")
    parser.add_argument("--status", action="store_true", help="show progress and exit")
    parser.add_argument("--rerun", action="store_true", help="ignore stored results")
    parser.add_argument("--reset", action="store_true", help="discard stored results and exit")
    parser.add_argument("--compare", action="store_true", help="write a cross-model comparison")
    args = parser.parse_args()

    if not TRUTH_PATH.is_file():
        raise SystemExit("[eval] run `python -m evals.ground_truth` first")

    truth = json.loads(TRUTH_PATH.read_text())
    all_cases = yaml.safe_load(GOLDEN_PATH.read_text())["cases"]

    if args.reset:
        results_path(resolve_model()).unlink(missing_ok=True)
        print("[eval] cleared stored results for this model")
        return 0

    if args.compare:
        write_comparison(all_cases)
        return 0

    model = resolve_model()
    store = {} if args.rerun else load_results(model)

    if args.status:
        print_status(all_cases, store)
        return 0

    cases = all_cases
    if args.category:
        cases = [case for case in cases if case["category"] == args.category]
        if not cases:
            categories = sorted({case["category"] for case in all_cases})
            raise SystemExit(f"[eval] no such category. Available: {', '.join(categories)}")

    # Already-recorded cases are skipped so batches accumulate rather than repeat.
    outstanding = [case for case in cases if case["id"] not in store]
    skipped = len(cases) - len(outstanding)
    if args.limit:
        outstanding = outstanding[: args.limit]

    if not outstanding:
        print(f"[eval] nothing outstanding ({skipped} already recorded)")
        write_report([from_record(record) for record in store.values()], args.tag, model)
        print_status(all_cases, store)
        return 0

    print(f"[eval] {len(outstanding)} case(s) against {model}", end="")
    print(f", {skipped} already recorded" if skipped else "")

    consecutive_unavailable = 0
    for index, case in enumerate(outstanding, 1):
        result = run_case(case, truth)

        # An unreachable provider is not evidence about accuracy. Leave the case
        # unrecorded so the next batch picks it up, and stop before the rest of
        # the quota is spent on calls that cannot succeed.
        if not result.passed and is_outage(result.actual):
            consecutive_unavailable += 1
            print(
                f"[eval] {index:>2}/{len(outstanding)} SKIP "
                f"{result.case_id} (provider unreachable)"
            )
            if consecutive_unavailable >= 2:
                print("\n[eval] the provider is unreachable — stopping so the quota is not wasted.")
                print("[eval] progress is saved; rerun this command when it is available again.")
                break
            continue

        # A deadline timeout is a real outcome for this model, so it is recorded
        # and the batch continues.
        if not result.passed and is_timeout(result.actual):
            result.reason = f"exceeded the per-question deadline ({result.reason})"

        consecutive_unavailable = 0
        store[result.case_id] = to_record(result, model)
        save_results(model, store)

        mark = "PASS" if result.passed else "FAIL"
        print(
            f"[eval] {index:>2}/{len(outstanding)} {mark} "
            f"{result.case_id} ({result.elapsed_s:.1f}s)"
        )
        if not result.passed:
            print(f"         {result.reason}")

    recorded = [from_record(record) for record in store.values()]
    write_report(recorded, args.tag, model)

    passed = sum(1 for record in store.values() if record["passed"])
    print(f"\n[eval] recorded {len(store)}/{len(all_cases)} cases, {passed} passing")
    print(f"[eval] wrote {REPORT_PATH.name} and {results_path(model).name}\n")
    print_status(all_cases, store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
