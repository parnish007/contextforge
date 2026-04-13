"""
Six-pillar radar chart: Stateless RAG vs ContextForge Nexus.
Sixth pillar is Slow-Drip Detection (new vs v1 paper).
"""
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path(__file__).parent / "fig_radar.png"

LABELS = [
    "Adversarial\nBlock Rate",
    "Failover\nLatency \u2193",
    "Token Noise\nReduction",
    "Context\nSurvival",
    "Benchmark\nPass Rate",
    "Slow-Drip\nDetection",
]
BASELINE = [0,    32,    0,    74,   68.3,  0   ]
NEXUS    = [85.0, 68.9, 87.4, 94.3, 100.0, 100.0]


def draw_radar(ax, values, color, label, alpha=0.25):
    N      = len(LABELS)
    angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
    vals   = values + [values[0]]
    angs   = angles + [angles[0]]
    ax.plot(angs, vals, "o-", lw=2, color=color, label=label)
    ax.fill(angs, vals, alpha=alpha, color=color)


def main():
    N      = len(LABELS)
    angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw=dict(polar=True))
    draw_radar(ax, BASELINE, "#95a5a6", "Stateless RAG Baseline")
    draw_radar(ax, NEXUS,    "#2471a3", "ContextForge Nexus", alpha=0.3)

    ax.set_xticks(angles)
    ax.set_xticklabels(LABELS, fontsize=8.5)
    ax.set_ylim(0, 100)
    ax.set_yticks([20, 40, 60, 80, 100])
    ax.set_yticklabels(["20", "40", "60", "80", "100"], fontsize=7)
    ax.legend(loc="upper right", bbox_to_anchor=(1.35, 1.15), fontsize=9)
    ax.set_title("Six-Pillar Safety Profile", fontsize=12, pad=20)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
