"""
ContextForge Nexus Architecture — Master Test Suite Runner
===================================================

Runs all six iteration suites sequentially (450 total tests) and
produces a unified pass/fail report with per-suite breakdown.

Usage:
    python -X utf8 benchmark/test_v5/run_all.py
    python -X utf8 benchmark/test_v5/run_all.py --suite iter_01_core
    python -X utf8 benchmark/test_v5/run_all.py --suite iter_04_scale iter_05_chaos
    python -X utf8 benchmark/test_v5/run_all.py --fast   # skip slow scale/chaos suites (runs 01-03, 06)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger
from benchmark.test_v5.nexus_tester_util import (
    ChaosConfig, MetricsCollector, run_suite, save_log,
)

# ── Suite registry ────────────────────────────────────────────────────────────

SUITES = [
    {
        "name":     "iter_01_core",
        "module":   "benchmark.test_v5.iter_01_core",
        "category": "networking",
        "label":    "01 · Networking & Circuit Breaker",
        "fast":     True,
    },
    {
        "name":     "iter_02_ledger",
        "module":   "benchmark.test_v5.iter_02_ledger",
        "category": "temporal_integrity",
        "label":    "02 · Temporal Integrity & Hash-Chain",
        "fast":     True,
    },
    {
        "name":     "iter_03_poison",
        "module":   "benchmark.test_v5.iter_03_poison",
        "category": "semantic_poison",
        "label":    "03 · Semantic Poison & Charter Guard",
        "fast":     True,
    },
    {
        "name":     "iter_04_scale",
        "module":   "benchmark.test_v5.iter_04_scale",
        "category": "rag_scale",
        "label":    "04 · RAG Flooding & Token Efficiency",
        "fast":     False,
    },
    {
        "name":     "iter_05_chaos",
        "module":   "benchmark.test_v5.iter_05_chaos",
        "category": "heat_death",
        "label":    "05 · Heat-Death Combined Chaos",
        "fast":     False,
    },
    {
        "name":     "iter_06_adversarial_boundary",
        "module":   "benchmark.test_v5.iter_06_adversarial_boundary",
        "category": "adversarial_boundary",
        "label":    "06 · Adversarial Boundary & Entropy Gate",
        "fast":     True,
    },
]


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_suite_by_name(suite_def: dict, cfg: ChaosConfig) -> dict:
    """Import a suite module, run its ALL_TESTS list, return summary dict."""
    mod = importlib.import_module(suite_def["module"])
    all_tests = getattr(mod, "ALL_TESTS")

    collector = MetricsCollector()
    t0 = time.monotonic()
    await run_suite(all_tests, collector, cfg, suite_def["category"])
    elapsed = (time.monotonic() - t0) * 1000

    log_path = save_log(collector, suite_def["name"])
    summary  = collector.summary()
    summary["suite"]     = suite_def["name"]
    summary["label"]     = suite_def["label"]
    summary["elapsed_ms"] = round(elapsed, 2)
    summary["log"]        = str(log_path)

    # Collect failures for the report
    summary["failures"] = [
        {"name": r.name, "error": r.error[:120]}
        for r in collector.results()
        if not r.passed
    ]
    return summary


async def main(args: argparse.Namespace) -> int:
    """Returns exit code: 0 = all pass, 1 = any failure."""
    logger.info("ContextForge Nexus Architecture — Master Test Suite Runner")
    logger.info(f"Python: {sys.version.split()[0]}")

    # Determine which suites to run
    if args.suite:
        targets = [s for s in SUITES if s["name"] in args.suite]
        if not targets:
            print(f"ERROR: Unknown suite(s): {args.suite}")
            print(f"Available: {[s['name'] for s in SUITES]}")
            return 1
    elif args.fast:
        targets = [s for s in SUITES if s["fast"]]
    else:
        targets = SUITES

    cfg = ChaosConfig(seed=42)

    print(f"\n{'━'*65}")
    print(f"  ContextForge Nexus — Nexus Test Suite ({len(targets)} suite(s))")
    print(f"{'━'*65}")
    print(f"  Running: {', '.join(s['name'] for s in targets)}\n")

    all_summaries: list[dict] = []
    total_pass = 0
    total_fail = 0

    for suite_def in targets:
        print(f"  ▶ {suite_def['label']} …")
        summary = await run_suite_by_name(suite_def, cfg)
        all_summaries.append(summary)
        total_pass += summary["passed"]
        total_fail += summary["failed"]
        bar   = "█" * int(summary["pass_rate"] * 20) + "░" * (20 - int(summary["pass_rate"] * 20))
        print(f"    [{bar}] {summary['passed']}/{summary['total']} "
              f"({summary['pass_rate']*100:.1f}%)  "
              f"avg {summary['mean_latency']}ms  "
              f"{summary['elapsed_ms']/1000:.1f}s\n")

    # ── Final report ──────────────────────────────────────────────────────────
    total = total_pass + total_fail
    overall_rate = total_pass / total if total else 0.0

    print(f"{'━'*65}")
    print(f"  OVERALL RESULTS — {len(targets)} suite(s) · {total} tests")
    print(f"{'━'*65}")
    print(f"  Passed:       {total_pass}  ({overall_rate*100:.1f}%)")
    print(f"  Failed:       {total_fail}")
    print()

    # Per-suite table
    print(f"  {'Suite':<28} {'Pass':>5} {'Fail':>5} {'Rate':>7}  {'Avg ms':>8}")
    print(f"  {'─'*28} {'─'*5} {'─'*5} {'─'*7}  {'─'*8}")
    for s in all_summaries:
        verdict = "✓" if s["failed"] == 0 else "✗"
        print(
            f"  {verdict} {s['label']:<27} "
            f"{s['passed']:>5} {s['failed']:>5} "
            f"{s['pass_rate']*100:>6.1f}%  "
            f"{s['mean_latency']:>7.1f}"
        )
    print()

    # Failure details
    all_failures = []
    for s in all_summaries:
        for f in s["failures"]:
            all_failures.append({"suite": s["suite"], **f})

    if all_failures:
        print(f"  FAILURES ({len(all_failures)}):")
        for f in all_failures[:20]:
            print(f"    ✗ [{f['suite']}] {f['name']}")
            if f["error"]:
                print(f"      {f['error'][:100]}")
        if len(all_failures) > 20:
            print(f"    … and {len(all_failures)-20} more (see logs/)")
        print()

    # Save combined report
    report_path = Path(__file__).parent / "logs" / "run_all_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "total":       total,
        "passed":      total_pass,
        "failed":      total_fail,
        "pass_rate":   round(overall_rate, 4),
        "suites":      all_summaries,
        "failures":    all_failures,
    }, indent=2, default=str), encoding="utf-8")
    print(f"  Combined report → {report_path}")
    print(f"{'━'*65}\n")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ContextForge Nexus Master Test Runner")
    parser.add_argument("--suite",  nargs="+", help="Run specific suites by name")
    parser.add_argument("--fast",   action="store_true", help="Run only fast suites (01–03)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
