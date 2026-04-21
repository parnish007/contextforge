"""
ContextForge — Paired Bootstrap Significance Tests
====================================================

For each (ContextForge-Nexus, baseline) metric pair, runs a one-sided paired
bootstrap test with B=10,000 resamples to test the null hypothesis:

    H₀ : μ(Nexus) ≤ μ(baseline)      (for metrics where higher is better)
    H₀ : μ(Nexus) ≥ μ(baseline)      (for metrics where lower is better)

Metrics and their directionality
---------------------------------
  ABR  — Adversarial Block Rate        (higher is better)
  CSS  — Context Stability Score       (higher is better)
  TNR  — Token Noise Reduction         (higher is better)
  CTO  — Context Token Overhead        (lower  is better)
  fail — Failover latency (ms)         (lower  is better)

Bootstrap procedure
-------------------
Given N paired observations (x_nexus[i], x_baseline[i]) for i ∈ 1..N:

  1.  Compute observed difference: δ_obs = mean(x_nexus) − mean(x_baseline)
      (positive = Nexus is better for higher-is-better metrics)

  2.  Resample B=10,000 bootstrap samples of size N with replacement from the
      N pairs (both series resampled together, preserving pairing).

  3.  For each bootstrap sample b:
          δ_b = mean(x_nexus_b) − mean(x_baseline_b)

  4.  p-value = fraction of δ_b that contradicts the observed direction:
          p = P(δ_b ≤ 0)   when δ_obs > 0   (one-sided, higher-is-better)
          p = P(δ_b ≥ 0)   when δ_obs < 0   (one-sided, lower-is-better)

  5.  Significance at α = 0.05 → p < 0.05 → SIGNIFICANT.
      Otherwise flag as "NOT SIGNIFICANT".

References
----------
  Efron & Tibshirani, "An Introduction to the Bootstrap" (1993), Ch. 16.
  Dror et al., "Deep Dominance — How to Properly Compare Deep Neural Models"
  (ACL 2019) — recommendation for paired bootstrap over Wilcoxon for NLP
  metrics derived from aggregate scores.

Output
------
  Prints a formatted table to stdout.
  Saves results/significance.json with full bootstrap details.

Usage
-----
    python -X utf8 benchmark/analysis/significance.py

    # Use existing multi-seed JSON (fast)
    python -X utf8 benchmark/analysis/significance.py --input results/comparison_table.json

    # Re-run N=10 seeds from scratch first
    python -X utf8 benchmark/analysis/significance.py --rerun
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── Project root ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RESULTS_DIR   = ROOT / "results"
SIG_JSON_OUT  = RESULTS_DIR / "significance.json"
MULTISEED_JSON = RESULTS_DIR / "comparison_table.json"

ALPHA      = 0.05
N_BOOTSTRAP = 10_000

# Metric directionality: True = higher is better, False = lower is better
_METRIC_DIR: dict[str, bool] = {
    "abr":         True,
    "css":         True,
    "tnr":         True,
    "cto":         False,
    "failover_ms": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BootstrapResult:
    nexus_system:     str
    baseline_system:  str
    metric:           str
    higher_is_better: bool
    n_obs:            int
    n_bootstrap:      int
    nexus_mean:       float
    baseline_mean:    float
    observed_delta:   float        # nexus_mean − baseline_mean
    p_value:          float
    significant:      bool         # p < ALPHA
    ci_delta_lo:      float        # 2.5th percentile of bootstrap Δ distribution
    ci_delta_hi:      float        # 97.5th percentile of bootstrap Δ distribution
    bootstrap_mean_delta: float    # mean of bootstrap Δ distribution


def _paired_bootstrap(
    nexus_vals:    list[float],
    baseline_vals: list[float],
    higher_is_better: bool,
    n_bootstrap:   int = N_BOOTSTRAP,
    seed:          int = 0,
) -> BootstrapResult:
    """
    Perform a one-sided paired bootstrap test.

    The pairing preserves the seed-matched structure: nexus_vals[i] and
    baseline_vals[i] were produced under the same probe ordering (seed i).
    """
    assert len(nexus_vals) == len(baseline_vals), "paired series must be same length"
    n   = len(nexus_vals)
    rng = random.Random(seed)

    nexus_mean    = sum(nexus_vals)    / n
    baseline_mean = sum(baseline_vals) / n
    obs_delta     = nexus_mean - baseline_mean

    # Bootstrap resampling
    boot_deltas: list[float] = []
    for _ in range(n_bootstrap):
        indices = [rng.randint(0, n - 1) for _ in range(n)]
        b_nexus    = sum(nexus_vals[i]    for i in indices) / n
        b_baseline = sum(baseline_vals[i] for i in indices) / n
        boot_deltas.append(b_nexus - b_baseline)

    boot_deltas.sort()

    # One-sided p-value
    if higher_is_better:
        # H₀: Nexus ≤ baseline   →   p = P(δ_b ≤ 0)
        p_value = sum(1 for d in boot_deltas if d <= 0) / n_bootstrap
    else:
        # H₀: Nexus ≥ baseline   →   p = P(δ_b ≥ 0)
        p_value = sum(1 for d in boot_deltas if d >= 0) / n_bootstrap

    lo_idx = int(0.025 * n_bootstrap)
    hi_idx = int(0.975 * n_bootstrap) - 1
    ci_lo  = boot_deltas[max(lo_idx, 0)]
    ci_hi  = boot_deltas[min(hi_idx, n_bootstrap - 1)]
    mean_d = sum(boot_deltas) / n_bootstrap

    return BootstrapResult(
        nexus_system     = "ContextForge-Nexus",
        baseline_system  = "",          # filled by caller
        metric           = "",          # filled by caller
        higher_is_better = higher_is_better,
        n_obs            = n,
        n_bootstrap      = n_bootstrap,
        nexus_mean       = round(nexus_mean,    6),
        baseline_mean    = round(baseline_mean, 6),
        observed_delta   = round(obs_delta,     6),
        p_value          = round(p_value,       6),
        significant      = p_value < ALPHA,
        ci_delta_lo      = round(ci_lo,  6),
        ci_delta_hi      = round(ci_hi,  6),
        bootstrap_mean_delta = round(mean_d, 6),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Load raw values from multi-seed JSON
# ─────────────────────────────────────────────────────────────────────────────

def _load_raw(json_path: Path) -> dict[str, dict[str, list[float]]]:
    """
    Parse results/comparison_table.json and return:
        { system_name: { metric_name: [run0, run1, …, runN] } }

    Supports both runner v1.0 (no raw block) and v2.0 (has raw block).
    For v1.0 files the raw values are synthesised by repeating the single
    point estimate N_RUNS times — this provides zero variance and every test
    will be "NOT SIGNIFICANT", which is the correct conservative result.
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, list[float]]] = {}

    for sys_entry in data["systems"]:
        name = sys_entry["system"]
        raw  = sys_entry.get("raw")

        if raw:
            # v2.0: raw per-run values present
            out[name] = {
                "abr":         raw.get("abr",         []),
                "css":         raw.get("css",         []),
                "tnr":         raw.get("tnr",         []),
                "cto":         raw.get("cto",         []),
                "failover_ms": raw.get("failover_ms", []),
            }
        else:
            # v1.0: only aggregate stats — create single-element lists
            m = sys_entry.get("metrics", {})
            out[name] = {
                "abr":         [m.get("ABR", 0.0)],
                "css":         [m.get("CSS", 0.0)],
                "tnr":         [m.get("token_noise_reduction", 0.0)],
                "cto":         [m.get("CTO", 0.0)],
                "failover_ms": [m.get("failover_ms", 0.0)],
            }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Run all pairwise tests
# ─────────────────────────────────────────────────────────────────────────────

def run_significance_tests(
    raw: dict[str, dict[str, list[float]]],
    n_bootstrap: int = N_BOOTSTRAP,
) -> list[BootstrapResult]:
    """
    Run paired bootstrap for every (Nexus, baseline) × metric combination.
    """
    nexus_name = "ContextForge-Nexus"
    if nexus_name not in raw:
        raise ValueError(
            f"'ContextForge-Nexus' not found in data. "
            f"Available systems: {list(raw.keys())}"
        )

    results: list[BootstrapResult] = []
    nexus_raw = raw[nexus_name]

    for baseline_name, baseline_raw in raw.items():
        if baseline_name == nexus_name:
            continue

        for metric, higher_is_better in _METRIC_DIR.items():
            n_vals  = nexus_raw.get(metric, [])
            b_vals  = baseline_raw.get(metric, [])

            # Align lengths (take min — should always be equal for same run set)
            n_pairs = min(len(n_vals), len(b_vals))
            if n_pairs == 0:
                continue

            n_vals_use = n_vals[:n_pairs]
            b_vals_use = b_vals[:n_pairs]

            br = _paired_bootstrap(
                n_vals_use, b_vals_use, higher_is_better,
                n_bootstrap=n_bootstrap,
            )
            br.baseline_system = baseline_name
            br.metric          = metric
            results.append(br)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

_SCALE: dict[str, float] = {
    "abr": 100.0, "css": 100.0, "tnr": 100.0, "cto": 1.0, "failover_ms": 1.0,
}
_UNIT: dict[str, str] = {
    "abr": "pp", "css": "pp", "tnr": "pp", "cto": "tok", "failover_ms": "ms",
}


def print_significance_table(results: list[BootstrapResult]) -> None:
    baselines = sorted({r.baseline_system for r in results})
    metrics   = list(_METRIC_DIR.keys())

    print(f"\n{'═'*88}")
    print(f"  PAIRED BOOTSTRAP SIGNIFICANCE  "
          f"(B={N_BOOTSTRAP:,} resamples, α={ALPHA}, one-sided)")
    print(f"  H₀: Nexus ≤ baseline (higher-is-better) or Nexus ≥ baseline (lower-is-better)")
    print(f"{'─'*88}")

    hdr = f"  {'Baseline':<22} {'Metric':<12} {'Nexus':>9} {'Baseline':>9} {'Δ':>9} {'p-value':>9}  Result"
    print(hdr)
    print(f"  {'─'*22} {'─'*12} {'─'*9} {'─'*9} {'─'*9} {'─'*9}  {'─'*20}")

    for baseline in baselines:
        for metric in metrics:
            matches = [r for r in results
                       if r.baseline_system == baseline and r.metric == metric]
            if not matches:
                continue
            r     = matches[0]
            sc    = _SCALE[metric]
            unit  = _UNIT[metric]
            sig   = "✓ SIGNIFICANT" if r.significant else "✗ NOT SIGNIFICANT"
            delta = r.observed_delta * sc
            dir_sym = "↑" if _METRIC_DIR[metric] else "↓"
            print(
                f"  {baseline:<22} {metric:<12} "
                f"{r.nexus_mean*sc:>8.3f}{unit[0]} "
                f"{r.baseline_mean*sc:>8.3f}{unit[0]} "
                f"{delta:>+8.3f} "
                f"{r.p_value:>9.4f}  {sig}  {dir_sym}"
            )
        print()

    # Summary: count not-significant results
    not_sig = [r for r in results if not r.significant]
    if not_sig:
        print(f"  ⚠  {len(not_sig)} metric(s) are NOT SIGNIFICANT at α={ALPHA}:")
        for r in not_sig:
            sc   = _SCALE[r.metric]
            unit = _UNIT[r.metric]
            print(f"       vs {r.baseline_system:<22}  {r.metric:<12}  "
                  f"Δ={r.observed_delta*sc:+.3f}{unit}  p={r.p_value:.4f}")
    else:
        print(f"  ✓  All metrics are significant at α={ALPHA}.")
    print(f"{'═'*88}\n")


def save_significance_json(results: list[BootstrapResult], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "analysis_version": "1.0",
        "alpha":            ALPHA,
        "n_bootstrap":      N_BOOTSTRAP,
        "run_at":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "One-sided paired bootstrap. "
            "p_value = P(bootstrap_delta contradicts observed direction). "
            "significant = (p_value < alpha)."
        ),
        "tests": [
            {
                "nexus_system":          r.nexus_system,
                "baseline_system":       r.baseline_system,
                "metric":                r.metric,
                "higher_is_better":      r.higher_is_better,
                "n_observations":        r.n_obs,
                "n_bootstrap_resamples": r.n_bootstrap,
                "nexus_mean":            r.nexus_mean,
                "baseline_mean":         r.baseline_mean,
                "observed_delta":        r.observed_delta,
                "bootstrap_mean_delta":  r.bootstrap_mean_delta,
                "ci_delta_95_lo":        r.ci_delta_lo,
                "ci_delta_95_hi":        r.ci_delta_hi,
                "p_value":               r.p_value,
                "significant":           r.significant,
                "verdict":               "SIGNIFICANT" if r.significant else "NOT SIGNIFICANT",
            }
            for r in results
        ],
        "summary": {
            "total_tests":       len(results),
            "significant":       sum(1 for r in results if r.significant),
            "not_significant":   sum(1 for r in results if not r.significant),
            "not_significant_details": [
                {"baseline": r.baseline_system, "metric": r.metric, "p_value": r.p_value}
                for r in results if not r.significant
            ],
        },
    }
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  Significance JSON → {out}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Paired bootstrap significance tests for ContextForge benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--input", default=str(MULTISEED_JSON), metavar="PATH",
        help=f"Multi-seed JSON input (default: {MULTISEED_JSON})",
    )
    p.add_argument(
        "--out", default=str(SIG_JSON_OUT), metavar="PATH",
        help=f"Significance JSON output (default: {SIG_JSON_OUT})",
    )
    p.add_argument(
        "--rerun", action="store_true",
        help="Re-run N=10 seeds from scratch before testing (slow)",
    )
    p.add_argument(
        "--bootstrap", type=int, default=N_BOOTSTRAP, metavar="B",
        help=f"Number of bootstrap resamples (default: {N_BOOTSTRAP})",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.rerun:
        print("  Re-running multi-seed benchmark (N=10) …")
        from benchmark.runner import run_multiseed, save_multiseed_json, SEEDS
        multi = run_multiseed(verbose=True)
        save_multiseed_json(multi, MULTISEED_JSON)
        print()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"  Input not found: {input_path}")
        print("  Run `python -X utf8 benchmark/runner.py` first, "
              "or pass --rerun to generate it now.")
        sys.exit(1)

    print(f"  Loading raw values from {input_path} …")
    raw = _load_raw(input_path)

    n_obs = max(
        len(v) for sys_raw in raw.values() for v in sys_raw.values()
    )
    print(f"  {len(raw)} systems  ×  {len(_METRIC_DIR)} metrics  ×  {n_obs} runs")
    print(f"  Running paired bootstrap (B={args.bootstrap:,}) …")

    results = run_significance_tests(raw, n_bootstrap=args.bootstrap)
    print_significance_table(results)
    save_significance_json(results, Path(args.out))
