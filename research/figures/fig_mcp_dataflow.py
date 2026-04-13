"""
MCP tool -> pillar mapping: shows all 22 tools grouped by pillar
in a clean categorical diagram.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path

OUT = Path(__file__).parent / "fig_mcp_dataflow.png"

PILLARS = {
    "Project\nManagement": {
        "tools": ["list_projects", "init_project", "rename_project",
                  "merge_projects", "delete_project", "project_stats"],
        "color": "#16a085",
    },
    "Decision\nGraph": {
        "tools": ["capture_decision", "load_context", "get_knowledge_node",
                  "list_decisions", "update_decision", "deprecate_decision", "link_decisions"],
        "color": "#2471a3",
    },
    "Tasks": {
        "tools": ["list_tasks", "create_task", "update_task"],
        "color": "#6c3483",
    },
    "Ledger\n& Sync": {
        "tools": ["rollback", "snapshot", "list_snapshots", "replay_sync", "list_events"],
        "color": "#922b21",
    },
    "Search": {
        "tools": ["search_context"],
        "color": "#196f3d",
    },
}


def main():
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 5.5)
    ax.axis("off")

    col_w = 2.4
    x0 = 0.2
    for i, (pillar, info) in enumerate(PILLARS.items()):
        x     = x0 + i * (col_w + 0.15)
        color = info["color"]
        tools = info["tools"]

        # Header
        rect = mpatches.FancyBboxPatch(
            (x, 4.6), col_w, 0.7,
            boxstyle="round,pad=0.04", facecolor=color, edgecolor="white", lw=1.2,
        )
        ax.add_patch(rect)
        ax.text(x + col_w / 2, 4.95, pillar,
                ha="center", va="center", color="white", fontsize=8.5, fontweight="bold")

        # Tool rows
        row_h = 0.52
        for j, tool in enumerate(tools):
            ty = 4.0 - j * row_h
            tr = mpatches.FancyBboxPatch(
                (x + 0.05, ty - 0.38), col_w - 0.1, 0.44,
                boxstyle="round,pad=0.02",
                facecolor=color, edgecolor="white", lw=0.8, alpha=0.35,
            )
            ax.add_patch(tr)
            ax.text(x + col_w / 2, ty - 0.16, tool,
                    ha="center", va="center", fontsize=7.0, color="#1a1a1a")

    ax.set_title("ContextForge MCP Server \u2014 22 Tools Mapped to Five Pillars", fontsize=11, pad=8)
    ax.text(6.5, 0.15,
            "22 tools total  \u00b7  Both Python (mcp/server.py) and TypeScript (mcp/index.ts) expose all 22",
            ha="center", fontsize=8, color="#555")
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
