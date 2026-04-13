"""
Ablation study grouped bar chart.
Data from research/benchmark_results/ablation_report.md (hardcoded — deterministic).
Metrics: CSS (Context Survival Score), ABR (Adversarial Block Rate).
"""
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

OUT = Path(__file__).parent / "fig_ablation.png"

CONDITIONS = [
    "Full System\n(Iter 5)",
    "\u2212Shadow-\nReviewer",
    "\u2212Historian\nGC",
    "\u2212L2 BM25",
    "\u2212Injection\nPatterns",
    "\u2212Noise\nTolerance",
    "Standard\nRAG",
]
CSS = [0.8124, 0.7201, 0.7943, 0.7055, 0.8124, 0.7841, 0.5891]
ABR = [100.0,    0.0,  100.0,  100.0,    0.0,  100.0,    0.0]


def main():
    x = np.arange(len(CONDITIONS))
    w = 0.35

    fig, ax1 = plt.subplots(figsize=(10, 4.2))
    ax2 = ax1.twinx()

    bars1 = ax1.bar(x - w / 2, CSS, w, color="#2980b9", alpha=0.8, label="CSS \u2191 (left axis)")
    bars2 = ax2.bar(x + w / 2, ABR, w, color="#e74c3c", alpha=0.8, label="ABR % \u2191 (right axis)")

    for bar in bars1:
        ax1.text(
            bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.005,
            f"{bar.get_height():.3f}", ha="center", va="bottom", fontsize=7.5, color="#2980b9",
        )
    for bar in bars2:
        ax2.text(
            bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
            f"{bar.get_height():.0f}%", ha="center", va="bottom", fontsize=7.5, color="#c0392b",
        )

    ax1.set_xticks(x)
    ax1.set_xticklabels(CONDITIONS, fontsize=8.5)
    ax1.set_ylabel("Context Survival Score (CSS)", color="#2980b9", fontsize=9)
    ax2.set_ylabel("Adversarial Block Rate (ABR %)", color="#c0392b", fontsize=9)
    ax1.set_ylim(0, 1.0)
    ax2.set_ylim(0, 130)
    ax1.set_title("Ablation Study \u2014 Per-Component Contribution to CSS and ABR", fontsize=11)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=8, loc="lower right")
    ax1.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
