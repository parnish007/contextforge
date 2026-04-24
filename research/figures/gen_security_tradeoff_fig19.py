# RATIONALE: Generate Figure 19 — Security trade-off curve (FPR vs Adversarial Recall).
"""
gen_security_tradeoff_fig19.py — Security trade-off scatter plot.

Figure 19 — Security trade-off curve:
  X axis: FPR (0% to 100%)
  Y axis: Adversarial Recall / ABR (0% to 100%)
  Three labeled points showing v1, v3, and HardenedRAG operating points.

Run: python research/figures/gen_security_tradeoff_fig19.py
Output → research/figures/output/fig_19_security_tradeoff.png (.pdf)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

ROOT    = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "research" / "figures" / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Data points ───────────────────────────────────────────────────────────────
# (FPR%, Recall%, label, color, marker, size)
POINTS = [
    (96.0,  95.0, "Paper Mode\n(v1, unusable in production)",  "#E53935", "^", 180),
    ( 1.0,  55.0, "ContextForge v3\n(deployed, FPR=1%)",       "#1565C0", "o", 220),
    ( 5.0,  75.0, "HardenedRAG\n(regex baseline)",             "#6A1FA2", "s", 160),
    ( 0.0,   0.0, "Stateless RAG\n(no guard)",                 "#9e9e9e", "x", 120),
]

# ── Ideal frontier (Pareto front illustration) ────────────────────────────────
# Hypothetical v4 projected points tracing a smooth frontier
frontier_fpr    = np.array([0, 1, 5, 10, 25, 50, 75, 96])
frontier_recall = np.array([0, 55, 75, 82, 90, 93, 95, 95])


def fig19():
    fig, ax = plt.subplots(figsize=(8, 6))

    # Draw frontier
    ax.plot(frontier_fpr, frontier_recall, "--", color="#78909C", linewidth=1.2,
            alpha=0.55, zorder=1, label="Security-recall frontier (schematic)")

    # Shade the "better than HardenedRAG" region
    ax.fill_between([0, 5], [75, 75], [100, 100],
                    alpha=0.04, color="#1565C0", label="_nolegend_")

    # Draw each point
    for fpr, recall, label, color, marker, size in POINTS:
        ax.scatter(fpr, recall, s=size, color=color, marker=marker,
                   zorder=5, edgecolors="black", linewidths=0.6)
        # Offset labels to avoid overlap
        xoff = {"^": 2.0, "o": 1.0, "s": 2.0, "x": 2.0}[marker]
        yoff = {"^": -6.0, "o": 3.0, "s": 3.5, "x": -5.0}[marker]
        ax.annotate(label,
                    xy=(fpr, recall),
                    xytext=(fpr + xoff, recall + yoff),
                    fontsize=8.5,
                    color=color,
                    fontweight="bold" if marker == "o" else "normal",
                    arrowprops=dict(arrowstyle="-", color=color, lw=0.8),
                    zorder=6)

    # Vertical line at FPR=5% to highlight production boundary
    ax.axvline(x=5, color="#78909C", linewidth=0.8, linestyle=":", alpha=0.6)
    ax.text(5.5, 5, "5% FPR\nboundary", fontsize=7.5, color="#78909C")

    # Annotation: v3 advantage callout
    ax.annotate("ContextForge v3 achieves\nlowest FPR at cost of recall\n(45% miss rate = future work)",
                xy=(1.0, 55.0),
                xytext=(18, 32),
                fontsize=8,
                color="#1565C0",
                arrowprops=dict(arrowstyle="->", color="#1565C0", lw=1.0),
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#1565C0", alpha=0.8))

    ax.set_xlabel("False Positive Rate — FPR (%, benign writes blocked)", fontsize=11)
    ax.set_ylabel("Adversarial Block Rate — ABR (%, attacks blocked)", fontsize=11)
    ax.set_xlim(-2, 102)
    ax.set_ylim(-5, 105)
    ax.set_xticks(range(0, 110, 10))
    ax.set_yticks(range(0, 110, 10))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v)}%"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v)}%"))
    ax.grid(linestyle="--", alpha=0.35, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Legend
    legend_elements = [
        mpatches.Patch(color="#E53935", label="Paper Mode v1 (96% FPR, 95% ABR)"),
        mpatches.Patch(color="#1565C0", label="ContextForge v3 (1% FPR, 55% ABR)"),
        mpatches.Patch(color="#6A1FA2", label="HardenedRAG (5% FPR, 75% ABR)"),
        mpatches.Patch(color="#9e9e9e", label="Stateless RAG (0% FPR, 0% ABR)"),
        plt.Line2D([0], [0], linestyle="--", color="#78909C", linewidth=1.2,
                   label="Security-recall frontier (schematic)"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", fontsize=8.5,
              framealpha=0.9, edgecolor="#ccc")

    ax.set_title(
        "Figure 19 — Security Trade-off Curve: FPR vs Adversarial Block Rate\n"
        "Suite 14 (300 samples). Lower-left = ideal. V3 minimises FPR at recall cost.",
        fontsize=12, fontweight="bold", pad=12)

    out = OUT_DIR / "fig_19_security_tradeoff.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    fig.savefig(out.with_suffix(".pdf"), dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    print("\nGenerating Figure 19 — Security Trade-off Curve...")
    fig19()
    print("Done.\n")
