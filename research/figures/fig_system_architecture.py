"""
Full system architecture: MCP client -> Transport -> five pillars -> DB/LLM.
Uses matplotlib patches for a clean block diagram.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
from pathlib import Path

OUT = Path(__file__).parent / "fig_system_architecture.png"


def box(ax, x, y, w, h, label, sub="", fc="#2c3e50", tc="white", fs=9):
    rect = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02",
                          facecolor=fc, edgecolor="white", linewidth=1.2, zorder=3)
    ax.add_patch(rect)
    if sub:
        ax.text(x + w / 2, y + h * 0.65, label, ha="center", va="center",
                color=tc, fontsize=fs, fontweight="bold", zorder=4)
        ax.text(x + w / 2, y + h * 0.28, sub, ha="center", va="center",
                color=tc, fontsize=fs - 2, zorder=4, alpha=0.85)
    else:
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
                color=tc, fontsize=fs, fontweight="bold", zorder=4)


def arrow(ax, x1, y1, x2, y2, label=""):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="->", color="#555", lw=1.4), zorder=5)
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ax.text(mx, my + 0.015, label, ha="center", fontsize=7, color="#555", zorder=6)


def main():
    fig, ax = plt.subplots(figsize=(12, 6.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.5)
    ax.axis("off")

    # IDE clients
    box(ax, 0.1, 4.5, 1.6, 1.6, "IDE Clients",
        "Claude Desktop\nCursor \u00b7 VS Code\nWindsurf", fc="#16a085")

    # MCP Transport (pillar 1)
    box(ax, 2.0, 4.5, 2.2, 1.6, "Transport",
        "Pillar 1\nStdio | SSE/HTTP\nJSON-RPC", fc="#2471a3")

    # Five pillars (2-5)
    box(ax, 4.5, 5.2, 2.2, 0.9, "Router",
        "Pillar 2\nGroq\u2192Gemini\u2192Ollama\nCircuit Breaker", fc="#6c3483")
    box(ax, 4.5, 4.2, 2.2, 0.9, "Memory",
        "Pillar 3\nAppend-only Ledger\nReviewerGuard", fc="#922b21")
    box(ax, 4.5, 3.2, 2.2, 0.9, "Retrieval",
        "Pillar 4\nDCI RAG\ncosine \u2265 0.75", fc="#1a5276")
    box(ax, 4.5, 2.2, 2.2, 0.9, "Sync",
        "Pillar 5\nAES-256-GCM\n15-min checkpoint", fc="#196f3d")

    # Backend resources
    box(ax, 7.1, 3.2, 2.0, 1.8, "SQLite DB",
        "decision_nodes\nhistorical_nodes\ntasks \u00b7 events", fc="#34495e")
    box(ax, 7.1, 5.1, 2.0, 0.9, "LLM Providers",
        "Groq | Gemini\nOllama (local)", fc="#784212")
    box(ax, 7.1, 2.2, 2.0, 0.9, ".forge/",
        "AES-256-GCM\nsnapshot files", fc="#117a65")

    # MCP Server dashed border
    ax.text(3.1, 6.3, "MCP Server  (mcp/server.py  \u00b7  mcp/index.ts)",
            ha="center", fontsize=9, color="#2471a3", fontweight="bold")
    ax.add_patch(mpatches.FancyBboxPatch(
        (1.9, 2.0), 2.5, 4.2,
        boxstyle="round,pad=0.05", fill=False,
        edgecolor="#2471a3", linewidth=1.5, linestyle="--", zorder=2,
    ))

    # Arrows: IDE -> Transport
    arrow(ax, 1.7, 5.3, 2.0, 5.3, "JSON-RPC")
    # Transport -> pillars
    for y_pos in [5.65, 4.65, 3.65, 2.65]:
        arrow(ax, 4.2, y_pos, 4.5, y_pos)
    # Pillars -> SQLite
    for y_pos in [5.65, 4.65, 3.65]:
        arrow(ax, 6.7, y_pos, 7.1, 4.1)
    arrow(ax, 6.7, 2.65, 7.1, 2.65)
    # Router -> LLM
    arrow(ax, 6.7, 5.65, 7.1, 5.55, "HTTP")

    # 8-Agent engine at bottom
    box(ax, 0.1, 0.2, 11.5, 1.7,
        "8-Agent RAT Engine  (python main.py)",
        "Sentry \u00b7 GhostCoder \u00b7 Librarian \u00b7 Shadow-Reviewer \u00b7 Historian \u00b7 PM \u00b7 Researcher \u00b7 Coder",
        fc="#1c2833", fs=9)
    ax.annotate("", xy=(5.9, 2.0), xytext=(5.9, 1.9),
                arrowprops=dict(arrowstyle="<->", color="#aaa", lw=1.2))

    ax.set_title("ContextForge Nexus \u2014 Full System Architecture", fontsize=12, pad=10)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
