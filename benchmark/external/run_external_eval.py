"""
ContextForge — External Adversarial Dataset Evaluation
=======================================================

Independent validation of the H*=3.5 entropy gate calibration using the
``deepset/prompt-injections`` dataset (CC-BY-4.0), a publicly available
benchmark of real prompt injection attacks and benign queries.

This script provides an out-of-sample check that the gate's F1 on the
internal probe set (paper: F1=1.0 at H*=3.5) generalises to independently
collected adversarial prompts.

Output
------
  research/benchmark_results/external_abr.json   — full results + metrics
  Printed summary to stdout

Warning
-------
If |paper_F1 - measured_macro_F1| > 0.05, prints:

    ⚠  THRESHOLD RECALIBRATION RECOMMENDED
       External macro-F1 = X.XX (paper F1 = 1.00, delta = 0.XX)
       Run: python -X utf8 src/security/gate_calibrator.py

Run
---
    python -X utf8 benchmark/external/run_external_eval.py

    # Full dataset (train + test, ~662 samples):
    python -X utf8 benchmark/external/run_external_eval.py --splits train test

    # Quick smoke-test (first 50 rows per split):
    python -X utf8 benchmark/external/run_external_eval.py --max 50

    # Offline (uses static fallback corpus, no internet):
    python -X utf8 benchmark/external/run_external_eval.py --offline
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmark.external.promptbench_adapter import (
    PromptBenchAdapter,
    ProbeRecord,
    PAPER_F1,
    F1_WARN_DELTA,
    DATASET_NAME,
)

RESULTS_DIR = ROOT / "research" / "benchmark_results"
OUT_FILE    = RESULTS_DIR / "external_abr.json"

# F1 the paper reports on its internal probe set
_PAPER_F1   = PAPER_F1
_WARN_DELTA = F1_WARN_DELTA


# ── Formatted output helpers ──────────────────────────────────────────────────

def _bar(rate: float, width: int = 30) -> str:
    filled = round(rate * width)
    return "█" * filled + "░" * (width - filled)


def _print_summary(metrics: dict[str, Any], source: str, elapsed: float) -> None:
    n    = metrics["n_total"]
    nadv = metrics["n_adversarial"]
    nben = metrics["n_benign"]

    bar_adv = _bar(metrics["block_rate_on_adversarial"])
    bar_ben = _bar(metrics["block_rate_on_benign"])
    bar_f1  = _bar(metrics["macro_f1"])

    print()
    print("=" * 65)
    print("  ContextForge External Adversarial Evaluation")
    print(f"  Dataset : {DATASET_NAME}  (source: {source})")
    print(f"  Samples : {n} total  ({nadv} adversarial · {nben} benign)")
    print(f"  Runtime : {elapsed:.1f}s")
    print("=" * 65)
    print()
    print(f"  Block rate — adversarial  {metrics['block_rate_on_adversarial']:.3f}  {bar_adv}")
    print(f"  Block rate — benign (FPR) {metrics['block_rate_on_benign']:.3f}  {bar_ben}")
    print(f"  Precision                 {metrics['precision']:.4f}")
    print(f"  Recall (TPR)              {metrics['recall']:.4f}")
    print(f"  F1 (attack class)         {metrics['f1_attack']:.4f}")
    print(f"  Macro-F1                  {metrics['macro_f1']:.4f}  {bar_f1}")
    print()

    reason = metrics.get("block_by_reason", {})
    if reason:
        print("  Block reasons:")
        for r, cnt in sorted(reason.items(), key=lambda x: -x[1]):
            print(f"    {r:<25} {cnt:>4}")
        print()

    # TP / FP / FN / TN confusion matrix
    tp, fp = metrics["tp"], metrics["fp"]
    fn, tn = metrics["fn"], metrics["tn"]
    print("  Confusion matrix:")
    print(f"    TP={tp:>4}  FN={fn:>4}   ← adversarial")
    print(f"    FP={fp:>4}  TN={tn:>4}   ← benign")
    print()


def _check_calibration(metrics: dict[str, Any]) -> bool:
    """Return True if recalibration is recommended."""
    measured = metrics["macro_f1"]
    delta    = abs(_PAPER_F1 - measured)
    if delta > _WARN_DELTA:
        print("  " + "─" * 61)
        print(f"  ⚠  THRESHOLD RECALIBRATION RECOMMENDED")
        print(f"     External macro-F1 = {measured:.4f}  (paper F1 = {_PAPER_F1:.2f}, delta = {delta:.4f})")
        print(f"     The entropy gate calibrated on the internal probe set")
        print(f"     does not generalise well to this external dataset.")
        print(f"     Run:  python -X utf8 src/security/gate_calibrator.py")
        print("  " + "─" * 61)
        print()
        return True
    else:
        print(f"  ✓  Calibration OK: external macro-F1={measured:.4f}  "
              f"(delta={delta:.4f} ≤ {_WARN_DELTA})")
        print()
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    splits  = args.splits
    offline = args.offline
    max_per = args.max

    print()
    print("[ExternalEval] Starting evaluation ...")
    if offline:
        print("[ExternalEval] --offline flag: using static fallback corpus")

    adapter = PromptBenchAdapter(
        use_huggingface = not offline,
        hf_splits       = splits,
        max_per_split   = max_per,
    )

    t0      = time.monotonic()
    records = adapter.run()
    elapsed = time.monotonic() - t0

    if not records:
        print("[ExternalEval] ERROR: No records produced. Check network or corpus.")
        sys.exit(1)

    metrics = PromptBenchAdapter.summary(records)
    _print_summary(metrics, adapter._source, elapsed)
    recal   = _check_calibration(metrics)

    # ── Per-sample detail: top FN (missed attacks) ────────────────────────
    fn_records = [r for r in records if r.is_fn]
    if fn_records:
        print(f"  False negatives (missed attacks) — top 10 by entropy:")
        sorted_fn = sorted(fn_records, key=lambda r: r.entropy, reverse=True)
        for r in sorted_fn[:10]:
            print(f"    H={r.entropy:.3f}  ρ={r.lz_density:.3f}  "
                  f'"{r.text_preview}"')
        print()

    # ── Per-sample detail: top FP (wrong blocks on benign) ────────────────
    fp_records = [r for r in records if r.is_fp]
    if fp_records:
        print(f"  False positives (benign blocked) — top 10 by entropy:")
        sorted_fp = sorted(fp_records, key=lambda r: r.entropy, reverse=True)
        for r in sorted_fp[:10]:
            print(f"    H={r.entropy:.3f}  ρ={r.lz_density:.3f}  "
                  f"reason={r.block_reason}  "
                  f'"{r.text_preview}"')
        print()

    # ── Save results ──────────────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    output: dict[str, Any] = {
        "eval_name":        "external_adversarial_eval",
        "dataset":          DATASET_NAME,
        "dataset_license":  "CC-BY-4.0",
        "source":           adapter._source,
        "splits_evaluated": splits,
        "paper_f1":         _PAPER_F1,
        "f1_warn_delta":    _WARN_DELTA,
        "recalibration_recommended": recal,
        "metrics":          metrics,
        "per_sample": [
            {
                "idx":          r.sample_idx,
                "split":        r.split,
                "true_label":   r.true_label,
                "predicted":    r.predicted_label,
                "entropy":      r.entropy,
                "lz_density":   r.lz_density,
                "blocked":      r.blocked,
                "block_reason": r.block_reason,
                "latency_ms":   r.latency_ms,
                "text_preview": r.text_preview,
            }
            for r in records
        ],
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    OUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"  Results saved → {OUT_FILE}")
    print()

    # Exit code: 0 = OK, 1 = recalibration recommended
    sys.exit(1 if recal else 0)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ContextForge external adversarial dataset evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--splits", nargs="+", default=["test"],
        metavar="SPLIT",
        help="HuggingFace splits to evaluate (default: test). "
             "Use 'train test' for full 662-sample evaluation.",
    )
    p.add_argument(
        "--max", type=int, default=0, metavar="N",
        help="Max rows per split (0 = all). Use 50 for a quick smoke-test.",
    )
    p.add_argument(
        "--offline", action="store_true",
        help="Skip HuggingFace download and use the static fallback corpus.",
    )
    p.add_argument(
        "--out", default=str(OUT_FILE), metavar="PATH",
        help=f"Output JSON path (default: {OUT_FILE})",
    )
    return p.parse_args()


if __name__ == "__main__":
    _args = _parse_args()
    # Allow --out to override the module-level constant
    OUT_FILE = Path(_args.out)
    main(_args)
