"""
ContextForge — Mock vs Live Drift Analysis
==========================================

Loads the mock multi-seed benchmark results (results/comparison_table.json)
and a live benchmark run (results/live_results.json), then computes per-metric
relative drift between the two modes.

Any metric whose drift exceeds DRIFT_THRESHOLD (default 10 %) is flagged with
a "DRIFT DETECTED" warning.  A machine-readable report is saved to
results/mock_vs_live_drift.json.

Metric mapping
--------------
The mock runner (benchmark/runner.py) produces adversarial-probe metrics for
five system variants.  The live runner (benchmark/live_runner.py) calls the
real NexusRouter with 50 representative questions.  The overlapping metrics
that can be meaningfully compared are:

  Mock metric          Live metric              Notes
  ─────────────────    ─────────────────────    ───────────────────────────────
  Nexus ABR (mean)     adversarial_pass_rate    Both measure adversarial handling
  Nexus failover_ms    mean_latency_ms          Simulated vs real; direction only
  Nexus CTO (mean)     mean_total_tokens        Token consumption per test

failover_ms / mean_latency_ms are expected to diverge significantly because
the mock uses simulated latency (80–350 ms random) while live measures real
network round-trips.  The script still reports this drift but marks it with
"[LATENCY — simulated vs real; drift expected]" so reviewers are not alarmed.

Usage
-----
  # Standard (loads results/comparison_table.json + results/live_results.json)
  python results/compare_mock_vs_live.py

  # Custom paths
  python results/compare_mock_vs_live.py \\
      --mock  results/comparison_table.json \\
      --live  results/live_results.json \\
      --out   results/mock_vs_live_drift.json

  # Raise or lower the drift threshold
  python results/compare_mock_vs_live.py --threshold 0.05
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "results"

DEFAULT_MOCK_PATH  = RESULTS_DIR / "comparison_table.json"
DEFAULT_LIVE_PATH  = RESULTS_DIR / "live_results.json"
DEFAULT_OUT_PATH   = RESULTS_DIR / "mock_vs_live_drift.json"

DRIFT_THRESHOLD: float = 0.10   # 10 % relative drift → flag


# ─────────────────────────────────────────────────────────────────────────────
# Loading helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_mock(path: Path) -> dict[str, Any]:
    """
    Parse comparison_table.json (runner.py v2.0 output).

    Returns a dict with Nexus metrics extracted:
      {
        "abr_mean":        float,
        "failover_ms_mean": float,
        "cto_mean":        float,
        "abr_ci95":        float,
        "failover_ms_ci95": float,
        "cto_ci95":        float,
        "n_runs":          int,
        "seeds":           list[int],
        "run_at":          str,
      }
    """
    raw: dict = json.loads(path.read_text(encoding="utf-8"))

    # Find Nexus system row (runner v2.0 uses "system" key; v1.0 uses "name")
    nexus_row: dict | None = None
    for sys_entry in raw.get("systems", []):
        name = sys_entry.get("system", sys_entry.get("name", ""))
        if "nexus" in name.lower():
            nexus_row = sys_entry
            break

    if nexus_row is None:
        raise ValueError(
            f"Could not find a Nexus system row in {path}. "
            f"Available: {[s.get('system', s.get('name')) for s in raw.get('systems', [])]}"
        )

    stats: dict = nexus_row.get("stats", {})

    def _mean(key: str) -> float:
        entry = stats.get(key, {})
        if isinstance(entry, dict):
            return float(entry.get("mean", entry.get("value", 0.0)))
        return float(entry)

    def _ci95(key: str) -> float:
        entry = stats.get(key, {})
        if isinstance(entry, dict):
            return float(entry.get("ci95", 0.0))
        return 0.0

    return {
        "abr_mean":          _mean("abr"),
        "abr_ci95":          _ci95("abr"),
        "failover_ms_mean":  _mean("failover_ms"),
        "failover_ms_ci95":  _ci95("failover_ms"),
        "cto_mean":          _mean("cto"),
        "cto_ci95":          _ci95("cto"),
        "n_runs":            raw.get("n_runs", 1),
        "seeds":             raw.get("seeds", []),
        "run_at":            raw.get("run_at", "unknown"),
        "nexus_description": nexus_row.get("description", ""),
    }


def _load_live(path: Path) -> dict[str, Any]:
    """
    Parse live_results.json (live_runner.py v1.0 output).

    Returns:
      {
        "pass_rate":              float,   # fraction of tests that returned a response
        "adversarial_pass_rate":  float,   # fraction of adversarial tests that passed
        "mean_latency_ms":        float,
        "mean_total_tokens":      float,   # mean of (prompt_tokens + response_tokens) per test
        "total_tests":            int,
        "adversarial_tests":      int,
        "run_at":                 str,
        "providers":              dict[str, int],
      }
    """
    raw: dict = json.loads(path.read_text(encoding="utf-8"))

    summary   = raw.get("summary", {})
    tests     = raw.get("tests",   [])

    # Adversarial tests — compute block rate
    adv_tests  = [t for t in tests if t.get("adversarial", False)]
    adv_passed = [t for t in adv_tests if t.get("passed", False)]
    adv_rate   = len(adv_passed) / max(len(adv_tests), 1)

    # Token consumption
    token_totals = [
        (t.get("prompt_tokens", 0) + t.get("response_tokens", 0))
        for t in tests if t.get("passed", False)
    ]
    mean_tokens = sum(token_totals) / max(len(token_totals), 1)

    # Provider breakdown
    providers: dict[str, int] = {}
    for t in tests:
        p = t.get("provider_used", "unknown")
        providers[p] = providers.get(p, 0) + 1

    return {
        "pass_rate":             float(summary.get("pass_rate", 0.0)),
        "adversarial_pass_rate": adv_rate,
        "mean_latency_ms":       float(summary.get("mean_latency_ms", 0.0)),
        "mean_total_tokens":     mean_tokens,
        "total_tests":           int(summary.get("total_tests", len(tests))),
        "adversarial_tests":     len(adv_tests),
        "run_at":                raw.get("run_at", "unknown"),
        "providers":             providers,
        "config":                raw.get("config", {}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Drift computation
# ─────────────────────────────────────────────────────────────────────────────

def _relative_drift(mock_val: float, live_val: float) -> float:
    """
    Compute relative drift = |live − mock| / |mock|.
    Returns 0.0 if mock_val is zero (avoid division by zero).
    """
    if abs(mock_val) < 1e-9:
        return 0.0 if abs(live_val) < 1e-9 else 1.0
    return abs(live_val - mock_val) / abs(mock_val)


_METRIC_DEFS = [
    # (label, mock_key, live_key, higher_is_better, latency_note)
    ("ABR / adversarial pass rate",
     "abr_mean",          "adversarial_pass_rate", True,  False),
    ("Mean latency (ms)",
     "failover_ms_mean",  "mean_latency_ms",        False, True),
    ("Mean tokens per test",
     "cto_mean",          "mean_total_tokens",       False, False),
]


def compute_drift(mock: dict, live: dict) -> list[dict]:
    """
    Compare mock vs live metrics.  Returns a list of drift records, one per
    comparable metric.
    """
    records: list[dict] = []
    threshold = DRIFT_THRESHOLD

    for label, mk_key, lv_key, hib, latency_note in _METRIC_DEFS:
        mock_val = mock.get(mk_key, 0.0)
        live_val = live.get(lv_key, 0.0)
        drift    = _relative_drift(mock_val, live_val)
        flagged  = drift > threshold

        note = ""
        if latency_note:
            note = "[LATENCY — simulated vs real; drift expected]"

        records.append({
            "metric":            label,
            "mock_key":          mk_key,
            "live_key":          lv_key,
            "mock_value":        round(mock_val, 6),
            "live_value":        round(live_val, 6),
            "relative_drift":    round(drift, 6),
            "drift_pct":         round(drift * 100, 2),
            "threshold_pct":     round(threshold * 100, 1),
            "flagged":           flagged,
            "note":              note,
            "higher_is_better":  hib,
        })

    return records


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

_COL = {
    "RESET": "\033[0m",
    "RED":   "\033[91m",
    "YEL":   "\033[93m",
    "GRN":   "\033[92m",
    "BOLD":  "\033[1m",
    "DIM":   "\033[2m",
}

# Disable colours when redirected to a file or non-TTY
if not sys.stdout.isatty():
    _COL = {k: "" for k in _COL}


def _flag_str(flagged: bool, drift_pct: float, note: str) -> str:
    if note:
        return f"{_COL['YEL']}DRIFT EXPECTED  {note}{_COL['RESET']}"
    if flagged:
        return f"{_COL['RED']}DRIFT DETECTED  ({drift_pct:.1f} % > threshold){_COL['RESET']}"
    return f"{_COL['GRN']}OK{_COL['RESET']}"


def print_drift_table(
    records: list[dict],
    mock_meta: dict,
    live_meta: dict,
) -> None:
    W = 70
    print(f"\n{'═'*W}")
    print(f"  {_COL['BOLD']}MOCK vs LIVE DRIFT ANALYSIS{_COL['RESET']}")
    print(f"{'─'*W}")
    print(f"  Mock run : {mock_meta.get('run_at','?')}  "
          f"(N={mock_meta.get('n_runs',1)} seeds)")
    print(f"  Live run : {live_meta.get('run_at','?')}  "
          f"({live_meta.get('total_tests',0)} tests, "
          f"providers: {live_meta.get('providers',{})})")
    print(f"  Drift threshold : {DRIFT_THRESHOLD*100:.0f} %")
    print(f"{'─'*W}")

    flagged_count = 0
    for r in records:
        flag = _flag_str(r["flagged"], r["drift_pct"], r["note"])
        drift_dir = ""
        if r["live_value"] > r["mock_value"]:
            drift_dir = "↑ live higher"
        elif r["live_value"] < r["mock_value"]:
            drift_dir = "↓ live lower"
        else:
            drift_dir = "= equal"

        print(f"\n  {_COL['BOLD']}{r['metric']}{_COL['RESET']}")
        print(f"    Mock  : {r['mock_value']:>12.4f}  (from {r['mock_key']})")
        print(f"    Live  : {r['live_value']:>12.4f}  (from {r['live_key']})")
        print(f"    Drift : {r['drift_pct']:>8.2f} %  {drift_dir}")
        print(f"    Status: {flag}")

        if r["flagged"] and not r["note"]:
            flagged_count += 1

    print(f"\n{'─'*W}")
    if flagged_count == 0:
        print(f"  {_COL['GRN']}All comparable metrics within {DRIFT_THRESHOLD*100:.0f} % drift tolerance.{_COL['RESET']}")
    else:
        print(f"  {_COL['RED']}{flagged_count} metric(s) exceed the {DRIFT_THRESHOLD*100:.0f} % drift threshold.{_COL['RESET']}")
        print(f"  Investigate whether mock calibration needs updating.")
    print(f"{'═'*W}\n")


def save_drift_report(
    records:   list[dict],
    mock_meta: dict,
    live_meta: dict,
    out:       Path,
) -> None:
    flagged = [r for r in records if r["flagged"] and not r["note"]]
    data = {
        "report_version":  "1.0",
        "drift_threshold": DRIFT_THRESHOLD,
        "any_drift":       len(flagged) > 0,
        "flagged_count":   len(flagged),
        "mock_meta": {
            "path":    str(DEFAULT_MOCK_PATH),
            "run_at":  mock_meta.get("run_at", "unknown"),
            "n_runs":  mock_meta.get("n_runs", 1),
            "seeds":   mock_meta.get("seeds", []),
        },
        "live_meta": {
            "path":         str(DEFAULT_LIVE_PATH),
            "run_at":       live_meta.get("run_at", "unknown"),
            "total_tests":  live_meta.get("total_tests", 0),
            "adv_tests":    live_meta.get("adversarial_tests", 0),
            "providers":    live_meta.get("providers", {}),
            "config":       live_meta.get("config", {}),
        },
        "metrics": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  Drift report → {out}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare mock vs live benchmark results and flag metric drift.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--mock", default=str(DEFAULT_MOCK_PATH), metavar="PATH",
                   help=f"Mock results JSON (default: {DEFAULT_MOCK_PATH.name})")
    p.add_argument("--live", default=str(DEFAULT_LIVE_PATH), metavar="PATH",
                   help=f"Live results JSON (default: {DEFAULT_LIVE_PATH.name})")
    p.add_argument("--out", default=str(DEFAULT_OUT_PATH), metavar="PATH",
                   help=f"Drift report output (default: {DEFAULT_OUT_PATH.name})")
    p.add_argument("--threshold", type=float, default=DRIFT_THRESHOLD, metavar="FRAC",
                   help="Relative drift threshold for flagging (default: 0.10 = 10 %%)")
    p.add_argument("--no-save", action="store_true",
                   help="Print the table but do not write the drift report JSON")
    return p.parse_args()


def main() -> None:
    global DRIFT_THRESHOLD

    args = _parse_args()
    DRIFT_THRESHOLD = args.threshold

    mock_path = Path(args.mock)
    live_path = Path(args.live)
    out_path  = Path(args.out)

    # ── Validate inputs ───────────────────────────────────────────────────────
    missing: list[str] = []
    if not mock_path.exists():
        missing.append(f"  Mock file not found : {mock_path}\n"
                       f"    Generate it with : python -X utf8 benchmark/runner.py")
    if not live_path.exists():
        missing.append(f"  Live file not found : {live_path}\n"
                       f"    Generate it with : python -X utf8 benchmark/live_runner.py")
    if missing:
        print("\nERROR — required input files are missing:\n")
        for m in missing:
            print(m)
        sys.exit(1)

    # ── Load ──────────────────────────────────────────────────────────────────
    try:
        mock_meta = _load_mock(mock_path)
    except Exception as exc:
        print(f"\nERROR loading mock results: {exc}\n")
        sys.exit(1)

    try:
        live_meta = _load_live(live_path)
    except Exception as exc:
        print(f"\nERROR loading live results: {exc}\n")
        sys.exit(1)

    # ── Compute ───────────────────────────────────────────────────────────────
    records = compute_drift(mock_meta, live_meta)

    # ── Report ────────────────────────────────────────────────────────────────
    print_drift_table(records, mock_meta, live_meta)

    if not args.no_save:
        save_drift_report(records, mock_meta, live_meta, out_path)

    # Exit code 1 if any non-latency metric drifts beyond threshold
    real_flags = [r for r in records if r["flagged"] and not r["note"]]
    sys.exit(1 if real_flags else 0)


if __name__ == "__main__":
    main()
