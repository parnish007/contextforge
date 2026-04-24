"""
Single entry point to generate all research figures.

Run from any directory:
    python research/figures/gen_all.py

Outputs 12 PNG files to research/figures/ at 300 DPI.
Requires: matplotlib, numpy (already in requirements.txt)
"""
import importlib.util
import sys
import time
from pathlib import Path

SCRIPTS = [
    "fig_entropy_gate",
    "fig_calibration_sweep",
    "fig_temporal_correlator",
    "fig_voh_tiers",
    "fig_ablation",
    "fig_token_savings",
    "fig_system_architecture",
    "fig_mcp_dataflow",
    "fig_radar",
    # v2.1 additions
    "fig_crdt_convergence",
    "fig_perplexity_gate",
    "fig_weight_sensitivity",
]

# v2.2 FPR-fix figures (Suite 14 / CF_MODE experiment).
# Generates figures 13-15 via a single script; run separately so main SCRIPTS
# list stays entry-per-figure compatible with load_and_run().
FPR_FIX_SCRIPT = "gen_fpr_fix_figures"


def load_and_run(name: str, script_dir: Path) -> None:
    path = script_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Generate all ContextForge paper figures")
    ap.add_argument("--fpr-only", action="store_true",
                    help="Generate only the FPR-fix figures (13-15) and skip the rest")
    ap.add_argument("--skip-fpr", action="store_true",
                    help="Skip the FPR-fix figures (13-15)")
    args = ap.parse_args()

    script_dir = Path(__file__).parent
    errors = []
    t0 = time.perf_counter()

    if not args.fpr_only:
        print(f"\nGenerating {len(SCRIPTS)} core figures -> {script_dir}\n")
        for name in SCRIPTS:
            try:
                load_and_run(name, script_dir)
            except Exception as exc:
                print(f"  ERROR in {name}: {exc}")
                errors.append(name)

    if not args.skip_fpr:
        print(f"\nGenerating FPR-fix figures (13-15) -> {script_dir}/output/\n")
        try:
            load_and_run(FPR_FIX_SCRIPT, script_dir)
        except Exception as exc:
            print(f"  ERROR in {FPR_FIX_SCRIPT}: {exc}")
            errors.append(FPR_FIX_SCRIPT)

    elapsed = time.perf_counter() - t0
    total   = (0 if args.fpr_only else len(SCRIPTS)) + (0 if args.skip_fpr else 1)
    print(f"\nAll {total} scripts processed in {elapsed:.1f}s"
          + (f"  ({len(errors)} errors)" if errors else ""))
    pngs = sorted(script_dir.glob("fig_*.png")) + sorted((script_dir / "output").glob("figure_*.png"))
    print(f"PNG files ({len(pngs)}):")
    for p in pngs:
        print(f"  {p.name:55s}  ({p.stat().st_size // 1024} KB)")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
