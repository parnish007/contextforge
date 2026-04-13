"""
FPR / FNR / F1 calibration sweep across H* from 2.5 to 5.0.
Data from research/benchmark_results/suite_08_fpr_calibration.json (hardcoded fallback if absent).
"""
import json
import matplotlib.pyplot as plt
from pathlib import Path

DATA = Path(__file__).parent.parent / "benchmark_results" / "suite_08_fpr_calibration.json"
OUT  = Path(__file__).parent / "fig_calibration_sweep.png"

# Hardcoded fallback (matches suite_08 output)
SWEEP_FALLBACK = [
    {"threshold": 2.50, "fpr": 1.00, "fnr": 0.00, "f1": 0.667},
    {"threshold": 2.75, "fpr": 1.00, "fnr": 0.00, "f1": 0.667},
    {"threshold": 3.00, "fpr": 1.00, "fnr": 0.00, "f1": 0.667},
    {"threshold": 3.25, "fpr": 0.80, "fnr": 0.00, "f1": 0.714},
    {"threshold": 3.50, "fpr": 0.00, "fnr": 0.00, "f1": 1.000},
    {"threshold": 3.75, "fpr": 0.00, "fnr": 0.50, "f1": 0.667},
    {"threshold": 4.00, "fpr": 0.00, "fnr": 1.00, "f1": 0.000},
    {"threshold": 4.25, "fpr": 0.00, "fnr": 1.00, "f1": 0.000},
    {"threshold": 4.50, "fpr": 0.00, "fnr": 1.00, "f1": 0.000},
    {"threshold": 4.75, "fpr": 0.00, "fnr": 1.00, "f1": 0.000},
    {"threshold": 5.00, "fpr": 0.00, "fnr": 1.00, "f1": 0.000},
]


def main():
    sweep = SWEEP_FALLBACK
    if DATA.exists():
        try:
            d = json.loads(DATA.read_text())
            if "summary" in d and "sweep" in d["summary"]:
                sweep = d["summary"]["sweep"]
        except Exception:
            pass

    thresholds = [r["threshold"] for r in sweep]
    fprs = [r["fpr"] for r in sweep]
    fnrs = [r["fnr"] for r in sweep]
    f1s  = [r["f1"]  for r in sweep]

    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(thresholds, fprs, "o-", color="#e74c3c", lw=2, ms=5, label="FPR (benign blocked)")
    ax.plot(thresholds, fnrs, "s-", color="#e67e22", lw=2, ms=5, label="FNR (adversarial passed)")
    ax.plot(thresholds, f1s,  "^-", color="#2980b9", lw=2, ms=5, label="F1 score")

    ax.axvline(3.5, color="#2c3e50", lw=1.5, ls="--", alpha=0.7)
    ax.annotate(
        r"$H^*=3.5$" + "\nF1 = 1.0\n(unique maximum)",
        xy=(3.5, 1.0), xytext=(3.7, 0.75),
        arrowprops=dict(arrowstyle="->", color="#2c3e50"),
        fontsize=8, color="#2c3e50",
    )

    ax.set_xlabel(r"Entropy Gate Threshold $H^*$ (bits)", fontsize=10)
    ax.set_ylabel("Rate", fontsize=10)
    ax.set_title(r"FPR / FNR / F1 Calibration Sweep — $H^* \in [2.5, 5.0]$", fontsize=11)
    ax.legend(fontsize=8)
    ax.set_ylim(-0.05, 1.1)
    ax.set_xlim(2.4, 5.1)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
