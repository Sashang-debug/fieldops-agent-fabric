"""Evaluation pipeline with LLM-native metrics and simple accuracy checks.

Runs support and finance suites and reports per-suite metrics.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fieldops.agents.finance_graph import run_finance_reconcile
from fieldops.agents.graph import run_support_workflow
from fieldops.rag.ingest import ingest_directory

SUPPORT_CASES = Path("data/eval/golden_cases.jsonl")
FINANCE_CASES = Path("data/eval/finance_cases.jsonl")


def _load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _score_support_case(case: dict, result: dict) -> dict:
    expected_category = case.get("expected_category", "").lower()
    actual_category = (result.get("category") or "").lower()
    category_ok = expected_category == actual_category

    must_contain = [s.lower() for s in case.get("answer_must_contain", [])]
    answer = (result.get("answer") or "").lower()
    content_ok = all(token in answer for token in must_contain)

    return {
        "id": case["id"],
        "category_ok": category_ok,
        "content_ok": content_ok,
        "passed": category_ok and content_ok,
        "metrics": result.get("metrics", {}),
    }


def _score_finance_case(case: dict, result: dict) -> dict:
    findings = result.get("findings") or []
    count = len(findings)

    if "expected_findings_count" in case:
        count_ok = count == int(case["expected_findings_count"])
    else:
        min_c = int(case.get("expected_findings_min", 0))
        max_c = int(case.get("expected_findings_max", 10_000))
        count_ok = min_c <= count <= max_c

    must_metrics = [m.lower() for m in case.get("must_flag_metrics", [])]
    flagged_metrics = {(f.get("metric") or "").lower() for f in findings}
    metrics_ok = all(m in flagged_metrics for m in must_metrics)

    must_types = case.get("must_finding_types", [])
    flagged_types = {f.get("finding_type") for f in findings}
    types_ok = all(t in flagged_types for t in must_types)

    return {
        "id": case["id"],
        "document_ref": case.get("document_ref"),
        "findings_count": count,
        "count_ok": count_ok,
        "metrics_ok": metrics_ok,
        "types_ok": types_ok,
        "passed": count_ok and metrics_ok and types_ok,
        "findings": findings,
        "metrics": result.get("metrics", {}),
    }


def _suite_totals(results: list[dict], *, suite: str, dataset: Path, wall_clock_s: float) -> dict[str, Any]:
    passed = sum(1 for r in results if r["passed"])
    total_tokens = sum(r.get("metrics", {}).get("total_tokens", 0) for r in results)
    total_cost = sum(r.get("metrics", {}).get("estimated_cost_usd", 0) for r in results)
    return {
        "suite": suite,
        "dataset": str(dataset),
        "cases": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "wall_clock_s": round(wall_clock_s, 2),
        "total_tokens": total_tokens,
        "total_estimated_cost_usd": round(total_cost, 4),
        "avg_tokens_per_case": round(total_tokens / len(results), 1) if results else 0,
        "results": results,
    }


def _run_suite(
    name: str,
    cases_path: Path,
    runner: Callable[[dict], dict],
    scorer: Callable[[dict, dict], dict],
) -> dict[str, Any]:
    cases = _load_cases(cases_path)
    started = time.perf_counter()
    results = [scorer(case, runner(case)) for case in cases]
    elapsed = time.perf_counter() - started
    return _suite_totals(results, suite=name, dataset=cases_path, wall_clock_s=elapsed)


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    # Usage:
    #   fieldops-eval              → both suites
    #   fieldops-eval support|finance
    #   fieldops-eval path/to/cases.jsonl
    selector = argv[0] if argv else "all"

    ingest_directory()
    suites: dict[str, dict[str, Any]] = {}

    if selector in {"all", "support"}:
        suites["support"] = _run_suite(
            "support",
            SUPPORT_CASES,
            runner=lambda case: run_support_workflow(
                user_query=case["query"],
                customer_id=case.get("customer_id", "cust-1001"),
            ),
            scorer=_score_support_case,
        )

    if selector in {"all", "finance"}:
        suites["finance"] = _run_suite(
            "finance",
            FINANCE_CASES,
            runner=lambda case: run_finance_reconcile(document_ref=case["document_ref"]),
            scorer=_score_finance_case,
        )

    if selector.endswith(".jsonl"):
        path = Path(selector)
        if "finance" in path.name:
            suites["finance"] = _run_suite(
                "finance",
                path,
                runner=lambda case: run_finance_reconcile(document_ref=case["document_ref"]),
                scorer=_score_finance_case,
            )
        else:
            suites["support"] = _run_suite(
                "support",
                path,
                runner=lambda case: run_support_workflow(
                    user_query=case["query"],
                    customer_id=case.get("customer_id", "cust-1001"),
                ),
                scorer=_score_support_case,
            )

    if not suites:
        print(json.dumps({"error": f"Unknown selector: {selector}"}, indent=2))
        sys.exit(2)

    total_cases = sum(s["cases"] for s in suites.values())
    total_passed = sum(s["passed"] for s in suites.values())
    report = {
        "suites": suites,
        "cases": total_cases,
        "passed": total_passed,
        "pass_rate": round(total_passed / total_cases, 3) if total_cases else 0.0,
        "wall_clock_s": round(sum(s.get("wall_clock_s", 0) for s in suites.values()), 2),
        "total_tokens": sum(s["total_tokens"] for s in suites.values()),
        "total_estimated_cost_usd": round(
            sum(s["total_estimated_cost_usd"] for s in suites.values()), 4
        ),
    }
    print(json.dumps(report, indent=2))
    sys.exit(0 if total_passed == total_cases else 1)


if __name__ == "__main__":
    main()
