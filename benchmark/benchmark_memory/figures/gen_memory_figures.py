# RATIONALE: Generate Suite 15 publication figures from benchmark results.
"""
gen_memory_figures.py — 3 publication-ready figures for Suite 15.

Figure 16 — Radar chart: MIS component breakdown per system (4 axes)
Figure 17 — Grouped bar: all 5 key metrics across 6 systems
Figure 18 — Heatmap: system × dataset performance matrix

Run: python benchmark/benchmark_memory/figures/gen_memory_figures.py
Outputs → benchmark/benchmark_memory/figures/output/
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

ROOT      = Path(__file__).resolve().parents[3]
LOGS_DIR  = ROOT / "benchmark" / "benchmark_memory" / "logs"
OUT_DIR   = ROOT / "benchmark" / "benchmark_memory" / "figures" / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Load results ──────────────────────────────────────────────────────────────
with open(LOGS_DIR / "suite_15_results.json", encoding="utf-8") as f:
    data = json.load(f)

results = data["results"]

SYSTEMS = ["StatelessRAG", "MemGPT", "LangGraph", "ClaudeMem", "HardenedRAG", "ContextForge"]
COLORS  = {
    "StatelessRAG": "#9e9e9e",
    "MemGPT":       "#2196F3",
    "LangGraph":    "#4CAF50",
    "ClaudeMem":    "#FF9800",
    "HardenedRAG":  "#9C27B0",
    "ContextForge": "#F44336",
}
LABELS  = {
    "StatelessRAG": "Stateless RAG",
    "MemGPT":       "MemGPT",
    "LangGraph":    "LangGraph",
    "ClaudeMem":    "ClaudeMem",
    "HardenedRAG":  "HardenedRAG",
    "ContextForge": "ContextForge v3",
}

# ─────────────────────────────────────────────────────────────────────────────
# Figure 16 — Radar chart (MIS components)
# ─────────────────────────────────────────────────────────────────────────────

def fig16_radar():
    axes   = ["Recall@3", "Update\nAccuracy", "Delete\nAccuracy", "Poison\nResistance"]
    n_axes = len(axes)
    angles = [n / n_axes * 2 * math.pi for n in range(n_axes)]
    angles += angles[:1]  # close polygon

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(axes, fontsize=11, fontweight="bold")
    ax.set_ylim(0, 1)
    ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_yticklabels(["0.2", "0.4", "0.6", "0.8", "1.0"], fontsize=8, color="grey")
    ax.yaxis.set_tick_params(labelsize=8)
    ax.grid(color="grey", linestyle="--", linewidth=0.5, alpha=0.7)
    ax.spines["polar"].set_visible(False)

    for name in SYSTEMS:
        row  = results[name]
        vals = [
            row["dataset_a"]["recall_at_3"],
            row["dataset_b"]["update_accuracy"],
            row["dataset_c"]["delete_accuracy"],
            row["dataset_d"]["poison_resistance"],
        ]
        vals += vals[:1]
        color  = COLORS[name]
        label  = LABELS[name]
        lw     = 2.5 if name == "ContextForge" else 1.5
        ls     = "-"  if name == "ContextForge" else "--"
        alpha  = 0.25 if name == "ContextForge" else 0.08

        ax.plot(angles, vals, linewidth=lw, linestyle=ls, color=color, label=label, zorder=3)
        ax.fill(angles, vals, alpha=alpha, color=color)

    # MIS legend (numeric)
    legend_lines = [
        mpatches.Patch(color=COLORS[n], label=f"{LABELS[n]} (MIS={results[n]['memory_integrity_score']:.3f})")
        for n in SYSTEMS
    ]
    ax.legend(handles=legend_lines, loc="upper right",
              bbox_to_anchor=(1.38, 1.15), fontsize=9, framealpha=0.9)

    ax.set_title("Figure 16 — Memory Integrity Score Components\n"
                 "Suite 15: 6 systems × 4 metrics (160 samples)",
                 fontsize=12, fontweight="bold", pad=20)

    out = OUT_DIR / "fig16_radar_memory_integrity.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out.relative_to(ROOT)}")


# ─────────────────────────────────────────────────────────────────────────────
# Figure 17 — Grouped bar chart (5 metrics × 6 systems)
# ─────────────────────────────────────────────────────────────────────────────

def fig17_grouped_bar():
    metric_keys  = ["recall_at_3", "update_accuracy", "delete_accuracy", "poison_resistance",
                    "memory_integrity_score"]
    metric_labels = ["Recall@3", "Update\nAccuracy", "Delete\nAccuracy", "Poison\nResistance",
                     "MIS\n(headline)"]

    n_metrics = len(metric_keys)
    n_systems = len(SYSTEMS)
    width     = 0.12
    x         = np.arange(n_metrics)

    fig, ax = plt.subplots(figsize=(12, 5.5))

    for i, name in enumerate(SYSTEMS):
        row = results[name]
        vals = [
            row["dataset_a"]["recall_at_3"],
            row["dataset_b"]["update_accuracy"],
            row["dataset_c"]["delete_accuracy"],
            row["dataset_d"]["poison_resistance"],
            row["memory_integrity_score"],
        ]
        offset = (i - n_systems / 2 + 0.5) * width
        bars   = ax.bar(x + offset, vals, width, color=COLORS[name],
                        label=LABELS[name], alpha=0.88, zorder=3)

        # Annotate MIS bar (last group) with value
        mis_bar = bars[4]
        ax.text(mis_bar.get_x() + mis_bar.get_width() / 2,
                mis_bar.get_height() + 0.012,
                f"{vals[4]:.2f}", ha="center", va="bottom", fontsize=7,
                color=COLORS[name], fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(metric_labels, fontsize=11)
    ax.set_ylim(0, 1.1)
    ax.set_ylabel("Score", fontsize=11)
    ax.set_title("Figure 17 — Memory Quality Benchmark: 5 Metrics × 6 Systems\n"
                 "Suite 15 (160 samples: 60 retrieval + 35 update + 30 delete + 35 poison)",
                 fontsize=12, fontweight="bold")
    ax.legend(fontsize=9, ncol=3, loc="upper left", framealpha=0.9)
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Highlight MIS column
    ax.axvspan(x[-1] - 0.42, x[-1] + 0.42, alpha=0.04, color="#F44336", zorder=0)

    out = OUT_DIR / "fig17_grouped_bar_metrics.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out.relative_to(ROOT)}")


# ─────────────────────────────────────────────────────────────────────────────
# Figure 18 — Heatmap (system × dataset)
# ─────────────────────────────────────────────────────────────────────────────

def fig18_heatmap():
    # Rows = systems, Cols = per-dataset scores + MIS
    col_labels  = ["Dataset A\nRecall@3", "Dataset B\nUpdate Acc",
                   "Dataset C\nDelete Acc", "Dataset D\nPoison Res",
                   "F1\n(Dataset A)", "MIS"]
    n_cols = len(col_labels)
    n_rows = len(SYSTEMS)

    matrix = np.zeros((n_rows, n_cols))
    for i, name in enumerate(SYSTEMS):
        row = results[name]
        matrix[i] = [
            row["dataset_a"]["recall_at_3"],
            row["dataset_b"]["update_accuracy"],
            row["dataset_c"]["delete_accuracy"],
            row["dataset_d"]["poison_resistance"],
            row["dataset_a"]["f1"],
            row["memory_integrity_score"],
        ]

    fig, ax = plt.subplots(figsize=(11, 5))
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")

    # Ticks
    ax.set_xticks(range(n_cols))
    ax.set_xticklabels(col_labels, fontsize=10, fontweight="bold")
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels([LABELS[n] for n in SYSTEMS], fontsize=10)

    # Annotate cells
    for i in range(n_rows):
        for j in range(n_cols):
            val      = matrix[i, j]
            fg_color = "white" if val < 0.35 or val > 0.85 else "black"
            fw       = "bold" if j == n_cols - 1 else "normal"
            ax.text(j, i, f"{val:.3f}", ha="center", va="center",
                    fontsize=9.5, color=fg_color, fontweight=fw)

    # Separator before MIS column
    ax.axvline(x=4.5, color="grey", linewidth=1.5, linestyle="--", alpha=0.6)

    plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Score (0–1)")
    ax.set_title("Figure 18 — Suite 15 Heatmap: System × Metric Performance\n"
                 "Green=high, Red=low; MIS = Memory Integrity Score (headline)",
                 fontsize=12, fontweight="bold", pad=14)

    out = OUT_DIR / "fig18_heatmap_system_dataset.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out.relative_to(ROOT)}")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nGenerating Suite 15 figures...")
    fig16_radar()
    fig17_grouped_bar()
    fig18_heatmap()
    print("Done.\n")
