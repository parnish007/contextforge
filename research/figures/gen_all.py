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


def load_and_run(name: str, script_dir: Path) -> None:
    path = script_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


def main():
    script_dir = Path(__file__).parent
    print(f"\nGenerating {len(SCRIPTS)} figures -> {script_dir}\n")
    t0 = time.perf_counter()
    errors = []
    for name in SCRIPTS:
        try:
            load_and_run(name, script_dir)
        except Exception as exc:
            print(f"  ERROR in {name}: {exc}")
            errors.append(name)
    elapsed = time.perf_counter() - t0
    print(f"\nAll {len(SCRIPTS)} figures processed in {elapsed:.1f}s"
          + (f"  ({len(errors)} errors)" if errors else ""))
    pngs = sorted(script_dir.glob("fig_*.png"))
    print(f"PNG files ({len(pngs)}):")
    for p in pngs:
        print(f"  {p.name:45s}  ({p.stat().st_size // 1024} KB)")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
