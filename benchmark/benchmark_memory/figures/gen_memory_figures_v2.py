# RATIONALE: Generate Suite 15 v2 publication figures from v2 benchmark results.
"""
gen_memory_figures_v2.py — 3 updated publication figures for Suite 15 v2.

Figure 16 v2 — Radar: MIS component breakdown (recency fix applied)
Figure 17 v2 — Grouped bar: all 5 key metrics across 6 systems
Figure 18 v2 — Heatmap: system × dataset performance matrix

Run: python benchmark/benchmark_memory/figures/gen_memory_figures_v2.py
Outputs → benchmark/benchmark_memory/figures/output/  (primary)
        → research/figures/output/                     (mirrored for pdflatex)
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

ROOT       = Path(__file__).resolve().parents[3]
V2_JSON    = ROOT / "benchmark" / "benchmark_memory" / "results" / "suite_15_final_report_v2.json"
OUT_DIR    = ROOT / "benchmark" / "benchmark_memory" / "figures" / "output"
PAPER_DIR  = ROOT / "research" / "figures" / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PAPER_DIR.mkdir(parents=True, exist_ok=True)


def _save_png(fig, name: str) -> None:
    """Save PNG to benchmark output and mirror to research/figures/output/ for pdflatex."""
    out = OUT_DIR / name
    fig.savefig(out, dpi=300, bbox_inches="tight")
    shutil.copy2(out, PAPER_DIR / name)
    print(f"  Saved: {out.relative_to(ROOT)}  →  research/figures/output/{name}")

# ── Load v2 results ────────────────────────────────────────────────────────────
with open(V2_JSON, encoding="utf-8") as f:
    report = json.load(f)

comp = report["systems_comparison"]

SYSTEMS = ["StatelessRAG", "MemGPT", "LangGraph", "ClaudeMem", "HardenedRAG", "ContextForge_v3"]
COLORS  = {
    "StatelessRAG":    "#9e9e9e",
    "MemGPT":          "#2196F3",
    "LangGraph":       "#4CAF50",
    "ClaudeMem":       "#FF9800",
    "HardenedRAG":     "#9C27B0",
    "ContextForge_v3": "#F44336",
}
LABELS = {
    "StatelessRAG":    "Stateless RAG",
    "MemGPT":          "MemGPT",
    "LangGraph":       "LangGraph",
    "ClaudeMem":       "ClaudeMem",
    "HardenedRAG":     "HardenedRAG",
    "ContextForge_v3": "ContextForge v3",
}

# ── Figure 16 v2 — Radar chart ────────────────────────────────────────────────

def fig16_radar_v2():
    axes   = ["Recall@3", "Update\nAccuracy", "Delete\nAccuracy", "Poison\nResistance"]
    n_axes = len(axes)
    angles = [n / n_axes * 2 * math.pi for n in range(n_axes)]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(axes, fontsize=11, fontweight="bold")
    ax.set_ylim(0, 1)
    ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_yticklabels(["0.2", "0.4", "0.6", "0.8", "1.0"], fontsize=8, color="grey")
    ax.grid(color="grey", linestyle="--", linewidth=0.5, alpha=0.7)
    ax.spines["polar"].set_visible(False)

    for name in SYSTEMS:
        row  = comp[name]
        vals = [
            row["retrieval_recall_at_3"],
            row["update_accuracy"],
            row["delete_accuracy"],
            row["poison_resistance"],
        ]
        vals += vals[:1]
        mis    = row["memory_integrity_score"]
        color  = COLORS[name]
        label  = LABELS[name]
        lw     = 2.5 if name == "ContextForge_v3" else 1.5
        ls     = "-"  if name == "ContextForge_v3" else "--"
        alpha  = 0.25 if name == "ContextForge_v3" else 0.08
        ax.plot(angles, vals, linewidth=lw, linestyle=ls, color=color, label=label, zorder=3)
        ax.fill(angles, vals, alpha=alpha, color=color)

    legend_lines = [
        mpatches.Patch(color=COLORS[n], label=f"{LABELS[n]} (MIS={comp[n]['memory_integrity_score']:.3f})")
        for n in SYSTEMS
    ]
    ax.legend(handles=legend_lines, loc="upper right",
              bbox_to_anchor=(1.42, 1.15), fontsize=9, framealpha=0.9)
    ax.set_title("Figure 16 v2 — Memory Integrity Score Components\n"
                 "Suite 15 v2: 6 systems × 4 metrics (160 samples, recency fix)",
                 fontsize=12, fontweight="bold", pad=20)

    _save_png(fig, "fig_16_memory_radar_v2.png")
    plt.close(fig)


# ── Figure 17 v2 — Grouped bar ────────────────────────────────────────────────

def fig17_grouped_bar_v2():
    metric_keys   = ["retrieval_recall_at_3", "update_accuracy", "delete_accuracy",
                     "poison_resistance", "memory_integrity_score"]
    metric_labels = ["Recall@3", "Update\nAccuracy", "Delete\nAccuracy",
                     "Poison\nResistance", "MIS\n(headline)"]

    n_metrics = len(metric_keys)
    n_systems = len(SYSTEMS)
    width     = 0.12
    x         = np.arange(n_metrics)

    fig, ax = plt.subplots(figsize=(13, 5.5))

    for i, name in enumerate(SYSTEMS):
        row  = comp[name]
        vals = [row[k] for k in metric_keys]
        offset = (i - n_systems / 2 + 0.5) * width
        bars   = ax.bar(x + offset, vals, width, color=COLORS[name],
                        label=LABELS[name], alpha=0.88,
                        linewidth=2.0 if name == "ContextForge_v3" else 0.5,
                        edgecolor="black" if name == "ContextForge_v3" else "none",
                        zorder=3)
        # Annotate MIS bar
        mis_bar = bars[4]
        ax.text(mis_bar.get_x() + mis_bar.get_width() / 2,
                mis_bar.get_height() + 0.012,
                f"{vals[4]:.2f}", ha="center", va="bottom", fontsize=7,
                color=COLORS[name], fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(metric_labels, fontsize=11)
    ax.set_ylim(0, 1.1)
    ax.set_ylabel("Score", fontsize=11)
    ax.set_title(
        "Figure 17 v2 — Memory Quality Benchmark: 5 Metrics × 6 Systems (recency fix)\n"
        "Suite 15 v2 (160 samples: 60 retrieval + 35 update + 30 delete + 35 poison)",
        fontsize=12, fontweight="bold")
    ax.legend(fontsize=9, ncol=3, loc="upper left", framealpha=0.9)
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.axvspan(x[-1] - 0.42, x[-1] + 0.42, alpha=0.04, color="#F44336", zorder=0)

    _save_png(fig, "fig_17_memory_bars_v2.png")
    plt.close(fig)


# ── Figure 18 v2 — Heatmap ────────────────────────────────────────────────────

def fig18_heatmap_v2():
    col_labels = ["Recall@3", "Update\nAcc", "Delete\nAcc", "Poison\nRes",
                  "F1\n(Recall)", "MIS"]
    n_cols = len(col_labels)
    n_rows = len(SYSTEMS)

    matrix = np.zeros((n_rows, n_cols))
    for i, name in enumerate(SYSTEMS):
        row = comp[name]
        matrix[i] = [
            row["retrieval_recall_at_3"],
            row["update_accuracy"],
            row["delete_accuracy"],
            row["poison_resistance"],
            row["retrieval_f1"],
            row["memory_integrity_score"],
        ]

    fig, ax = plt.subplots(figsize=(11, 5))
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")

    ax.set_xticks(range(n_cols))
    ax.set_xticklabels(col_labels, fontsize=10, fontweight="bold")
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels([LABELS[n] for n in SYSTEMS], fontsize=10)

    for i in range(n_rows):
        for j in range(n_cols):
            val      = matrix[i, j]
            fg_color = "white" if val < 0.35 or val > 0.85 else "black"
            fw       = "bold" if j == n_cols - 1 else "normal"
            ax.text(j, i, f"{val:.3f}", ha="center", va="center",
                    fontsize=9.5, color=fg_color, fontweight=fw)

    ax.axvline(x=4.5, color="grey", linewidth=1.5, linestyle="--", alpha=0.6)
    plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Score (0–1)")
    ax.set_title(
        "Figure 18 v2 — Suite 15 Heatmap: System × Metric Performance (recency fix)\n"
        "Green=high, Red=low; MIS = Memory Integrity Score (headline)",
        fontsize=12, fontweight="bold", pad=14)

    _save_png(fig, "fig_18_memory_heatmap_v2.png")
    plt.close(fig)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nGenerating Suite 15 v2 figures...")
    fig16_radar_v2()
    fig17_grouped_bar_v2()
    fig18_heatmap_v2()
    print("Done.\n")
