# RATIONALE: Sensitivity analysis for the Weighted Composite Safety Index Φ.
# Sweeps adversarial_weight from 0.2–0.8 in 0.1 steps; for each combination
# of (w_abr, w_lat, w_tnr) that sums to 1.0, recomputes Φ using the Nexus
# multi-seed results from results/comparison_table.json.
"""
Weight Sensitivity Analysis for the Safety Index Φ
====================================================

The Weighted Composite Safety Index

    Φ = w₀·ABR + w₁·Δlatency + w₂·TNR

uses three weights that sum to 1.  This script sweeps adversarial_weight
(w₀) from 0.2 to 0.8 in 0.1 steps.  For each w₀, the remaining weight
(1 − w₀) is split evenly between w₁ and w₂ — but the full 2-D grid over
(w₁, w₂ | w₀) is also explored (see ``--full-grid``).

Outputs
-------
  results/weight_sensitivity.csv
      Rows: one per (w_abr, w_lat, w_tnr) triple.
      Columns: w_abr, w_lat, w_tnr, phi_nexus, phi_stateless, phi_hardened,
               phi_memgpt, phi_langchain, nexus_delta_vs_stateless.

  results/figures/weight_sensitivity_heatmap.png
      2-D heatmap of Φ(Nexus) − Φ(StatelessRAG) over the (w_abr, w_tnr)
      grid with w_lat = 1 − w_abr − w_tnr.  Invalid combinations (negative
      w_lat) are masked.

Usage
-----
  # Standard sweep (uses results/comparison_table.json)
  python -X utf8 benchmark/analysis/weight_sensitivity.py

  # Full 2-D grid instead of 1-D adversarial sweep
  python -X utf8 benchmark/analysis/weight_sensitivity.py --full-grid

  # Custom mock results path
  python -X utf8 benchmark/analysis/weight_sensitivity.py \\
      --input results/comparison_table.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from itertools import product
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.metrics.safety_index import WeightedSafetyIndex

RESULTS_DIR = ROOT / "results"
FIGURES_DIR = RESULTS_DIR / "figures"

DEFAULT_INPUT  = RESULTS_DIR / "comparison_table.json"
DEFAULT_CSV    = RESULTS_DIR / "weight_sensitivity.csv"
DEFAULT_PNG    = FIGURES_DIR / "weight_sensitivity_heatmap.png"

# Latency normalisation constant = worst baseline latency (ms)
_LATENCY_MAX_MS: float = 480.0

# Sweep range for adversarial weight
_W_ABR_VALUES: list[float] = [round(v * 0.1, 1) for v in range(2, 9)]  # 0.2..0.8


# ─────────────────────────────────────────────────────────────────────────────
# Load mock results
# ─────────────────────────────────────────────────────────────────────────────

def _load_system_metrics(path: Path) -> dict[str, dict[str, float]]:
    """
    Parse comparison_table.json and return per-system mean metrics.

    Returns
    -------
    {system_name: {abr, tnr, failover_ms}}
    """
    raw: dict = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, float]] = {}

    for entry in raw.get("systems", []):
        name  = entry.get("system", entry.get("name", "unknown"))
        stats = entry.get("stats", {})

        def _m(key: str) -> float:
            v = stats.get(key, {})
            if isinstance(v, dict):
                return float(v.get("mean", v.get("value", 0.0)))
            return float(v)

        result[name] = {
            "abr":        _m("abr"),
            "tnr":        _m("tnr"),
            "failover_ms": _m("failover_ms"),
        }

    if not result:
        raise ValueError(f"No system entries found in {path}.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Weight combinations
# ─────────────────────────────────────────────────────────────────────────────

def _generate_sweep_weights() -> list[tuple[float, float, float]]:
    """
    1-D sweep: for each w_abr in 0.2..0.8, split remainder evenly between
    w_lat and w_tnr (rounding to avoid floating-point drift).
    """
    combos: list[tuple[float, float, float]] = []
    for w0 in _W_ABR_VALUES:
        rem  = round(1.0 - w0, 10)
        w1   = round(rem / 2.0, 10)
        w2   = round(rem - w1, 10)
        combos.append((w0, w1, w2))
    return combos


def _generate_full_grid(step: float = 0.1) -> list[tuple[float, float, float]]:
    """
    2-D grid over (w_abr, w_tnr) with w_lat = 1 − w_abr − w_tnr.
    Only combinations where all three weights ≥ 0 are returned.
    """
    vals   = [round(i * step, 10) for i in range(0, int(1 / step) + 1)]
    combos: list[tuple[float, float, float]] = []
    for w0, w2 in product(vals, repeat=2):
        w1 = round(1.0 - w0 - w2, 10)
        if w1 < -1e-9:
            continue
        w1 = max(0.0, w1)
        if abs(w0 + w1 + w2 - 1.0) < 1e-6:
            combos.append((round(w0, 2), round(w1, 2), round(w2, 2)))
    return combos


# ─────────────────────────────────────────────────────────────────────────────
# Compute Φ for each system under each weight combination
# ─────────────────────────────────────────────────────────────────────────────

def compute_sensitivity_table(
    systems: dict[str, dict[str, float]],
    weight_combos: list[tuple[float, float, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for w0, w1, w2 in weight_combos:
        try:
            idx = WeightedSafetyIndex(weights=(w0, w1, w2), profile="custom")
        except ValueError:
            continue

        row: dict[str, Any] = {
            "w_abr": w0,
            "w_lat": w1,
            "w_tnr": w2,
        }
        for sname, m in systems.items():
            r = idx.compute(
                abr            = m["abr"],
                latency_ms     = m["failover_ms"],
                latency_max_ms = _LATENCY_MAX_MS,
                tnr            = m["tnr"],
            )
            col = sname.lower().replace(" ", "_").replace("-", "_")
            row[f"phi_{col}"] = round(r.phi, 4)

        # Nexus vs StatelessRAG delta (both keys may not exist in every file)
        nexus_key    = next((k for k in row if "nexus" in k.lower()),    None)
        stateless_key = next((k for k in row if "stateless" in k.lower()), None)
        if nexus_key and stateless_key:
            row["nexus_delta_vs_stateless"] = round(
                row[nexus_key] - row[stateless_key], 4
            )
        else:
            row["nexus_delta_vs_stateless"] = None

        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Save CSV
# ─────────────────────────────────────────────────────────────────────────────

def save_csv(rows: list[dict[str, Any]], out: Path) -> None:
    if not rows:
        print("  No rows to save.")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  Weight sensitivity CSV → {out}")


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

def save_heatmap(rows: list[dict[str, Any]], out: Path) -> None:
    """
    Plot Φ(Nexus) − Φ(StatelessRAG) as a 2-D heatmap over (w_abr, w_tnr).

    Requires matplotlib.  If not installed, prints a warning and skips.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np
    except ImportError:
        print("  [heatmap skipped — matplotlib not installed]")
        return

    # Extract grid values
    w_abr_set = sorted({r["w_abr"] for r in rows})
    w_tnr_set = sorted({r["w_tnr"] for r in rows})

    # Build delta matrix (rows=w_tnr, cols=w_abr) for imshow
    matrix = np.full((len(w_tnr_set), len(w_abr_set)), np.nan)

    abr_idx = {v: i for i, v in enumerate(w_abr_set)}
    tnr_idx = {v: i for i, v in enumerate(w_tnr_set)}

    for r in rows:
        delta = r.get("nexus_delta_vs_stateless")
        if delta is None:
            continue
        ai = abr_idx.get(r["w_abr"])
        ti = tnr_idx.get(r["w_tnr"])
        if ai is not None and ti is not None:
            matrix[ti, ai] = delta

    # Plot
    fig, ax = plt.subplots(figsize=(9, 7))

    vmax = float(np.nanmax(np.abs(matrix))) if not np.all(np.isnan(matrix)) else 1.0
    norm = mcolors.TwoSlopeNorm(vmin=-vmax, vcenter=0.0, vmax=vmax)
    im   = ax.imshow(matrix, aspect="auto", origin="lower",
                     cmap="RdYlGn", norm=norm,
                     extent=[
                         w_abr_set[0] - 0.05, w_abr_set[-1] + 0.05,
                         w_tnr_set[0] - 0.05, w_tnr_set[-1] + 0.05,
                     ])

    # Mask out NaN (invalid weight combos where w_lat < 0)
    mask = np.isnan(matrix)
    mask_overlay = np.ma.array(np.zeros_like(matrix), mask=~mask)
    ax.imshow(mask_overlay, aspect="auto", origin="lower",
              cmap="gray", alpha=0.3,
              extent=[
                  w_abr_set[0] - 0.05, w_abr_set[-1] + 0.05,
                  w_tnr_set[0] - 0.05, w_tnr_set[-1] + 0.05,
              ])

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Φ(Nexus) − Φ(StatelessRAG)", fontsize=12)

    ax.set_xlabel("w_abr  (adversarial weight)", fontsize=12)
    ax.set_ylabel("w_tnr  (token-noise weight)",  fontsize=12)
    ax.set_title(
        "Safety Index Sensitivity: Φ(Nexus) − Φ(Stateless)\n"
        "w_lat = 1 − w_abr − w_tnr  (white = invalid combination)",
        fontsize=12,
    )

    # Overlay preset markers
    presets_plot = [
        ("ide_workflow",       0.5, 0.2, "★"),
        ("backend_automation", 0.3, 0.3, "▲"),
        ("research_pipeline",  0.4, 0.4, "●"),
    ]
    for label, wa, wt, marker in presets_plot:
        ax.plot(wa, wt, marker, color="black", markersize=14,
                label=f"{label} ({wa},{1.0-wa-wt:.1f},{wt})")
    ax.legend(fontsize=9, loc="upper right")

    plt.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=150)
    plt.close(fig)
    print(f"  Heatmap PNG         → {out}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Weight sensitivity analysis for the Safety Index Φ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--input",      default=str(DEFAULT_INPUT),  metavar="PATH",
                   help="Mock results JSON (default: results/comparison_table.json)")
    p.add_argument("--out-csv",    default=str(DEFAULT_CSV),    metavar="PATH",
                   help="Output CSV path")
    p.add_argument("--out-png",    default=str(DEFAULT_PNG),    metavar="PATH",
                   help="Output heatmap PNG path")
    p.add_argument("--full-grid",  action="store_true",
                   help="Explore full 2-D (w_abr, w_tnr) grid instead of 1-D sweep")
    p.add_argument("--step",       type=float, default=0.1, metavar="STEP",
                   help="Grid step for --full-grid mode (default: 0.1)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    inp  = Path(args.input)

    if not inp.exists():
        print(f"\nERROR: Input file not found: {inp}")
        print("  Generate it first:\n    python -X utf8 benchmark/runner.py\n")
        sys.exit(1)

    print(f"\n  Loading mock results from {inp.name} …")
    systems = _load_system_metrics(inp)
    print(f"  Systems found: {list(systems)}")

    if args.full_grid:
        combos = _generate_full_grid(step=args.step)
        print(f"  Grid mode: {len(combos)} weight combinations (step={args.step})")
    else:
        combos = _generate_sweep_weights()
        print(f"  Sweep mode: {len(combos)} combinations "
              f"(w_abr={_W_ABR_VALUES})")

    rows = compute_sensitivity_table(systems, combos)
    print(f"  Computed Φ for {len(rows)} combinations × {len(systems)} systems\n")

    # Print summary table
    print(f"  {'w_abr':>6}  {'w_lat':>6}  {'w_tnr':>6}  "
          f"{'Φ(Nexus)':>10}  {'Δ vs Stateless':>15}")
    print(f"  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*10}  {'─'*15}")
    for r in rows:
        nexus_key = next((k for k in r if "nexus" in k.lower() and k.startswith("phi_")), None)
        phi_n = f"{r[nexus_key]:.4f}" if nexus_key else "N/A"
        delta = r.get("nexus_delta_vs_stateless")
        delta_s = f"+{delta:.4f}" if delta is not None and delta >= 0 else (f"{delta:.4f}" if delta is not None else "N/A")
        print(f"  {r['w_abr']:>6.2f}  {r['w_lat']:>6.2f}  {r['w_tnr']:>6.2f}  "
              f"{phi_n:>10}  {delta_s:>15}")

    save_csv(rows, Path(args.out_csv))

    if args.full_grid:
        save_heatmap(rows, Path(args.out_png))
    else:
        # Even in sweep mode, generate the full grid for the heatmap
        full_rows = compute_sensitivity_table(systems, _generate_full_grid(step=0.1))
        save_heatmap(full_rows, Path(args.out_png))


if __name__ == "__main__":
    main()
