"""
ContextForge v3.0 — Omega-Pulse Terminal Dashboard (v2)

A cinematic, minimalist terminal UI (Linear/Vercel aesthetic) built with Rich.
Renders agent status, recent decision nodes, L1 cache statistics,
active PM tasks, and the Researcher's live research feed.

Usage (called from main.py):
    from src.core.dashboard import OmegaDashboard
    dash = OmegaDashboard(librarian, db_path="data/contextforge.db")
    dash.render()          # single-shot render
    dash.live_loop()       # auto-refresh every N seconds
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.agents.librarian import LibrarianAgent
    from src.agents.pm import PMAgent

try:
    from rich import box
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False
    logger.warning("Dashboard: rich not installed — pip install rich")


# ---------------------------------------------------------------------------
# Agent state registry (updated by main.py as agents start/stop)
# ---------------------------------------------------------------------------

_AGENT_REGISTRY: dict[str, dict] = {
    "Sentry":    {"status": "offline", "emoji": "🔭", "role": "File Watcher"},
    "Librarian": {"status": "offline", "emoji": "📚", "role": "Memory Keeper"},
    "GhostCoder":{"status": "offline", "emoji": "🧠", "role": "LLM Distiller"},
    "PM":              {"status": "offline", "emoji": "📋", "role": "Project Manager (Phase 2)"},
    "Researcher":      {"status": "offline", "emoji": "🔎", "role": "Web Researcher (Phase 2)"},
    "Shadow-Reviewer": {"status": "pending", "emoji": "🔍", "role": "Auditor (Phase 2)"},
    "Token-Gater":     {"status": "pending", "emoji": "⚡", "role": "Cost Optimizer (Phase 2)"},
    "Historian":       {"status": "pending", "emoji": "📜", "role": "Truth Keeper (Phase 2)"},
    "Sync-Master":     {"status": "pending", "emoji": "🔄", "role": "CRDT Sync (Phase 3)"},
    "Architect":       {"status": "pending", "emoji": "🏛", "role": "Lead Reasoner (Phase 4)"},
}

def set_agent_online(name: str) -> None:
    """Called from main.py when an agent successfully starts."""
    if name in _AGENT_REGISTRY:
        _AGENT_REGISTRY[name]["status"] = "online"

def set_agent_offline(name: str) -> None:
    if name in _AGENT_REGISTRY:
        _AGENT_REGISTRY[name]["status"] = "offline"


# ---------------------------------------------------------------------------
# OmegaDashboard
# ---------------------------------------------------------------------------

class OmegaDashboard:
    """
    Parameters
    ----------
    librarian : LibrarianAgent | None
        For reading L1 cache stats directly from memory.
    db_path : str
        Path to the SQLite DB for recent-node queries.
    refresh_seconds : int
        Auto-refresh interval for live_loop().
    project_id : str | None
        Filter nodes by project in the recent-nodes panel.
    """

    def __init__(
        self,
        librarian: "LibrarianAgent | None" = None,
        db_path: str = "data/contextforge.db",
        refresh_seconds: int = 5,
        project_id: str | None = None,
        storage=None,   # StorageAdapter | None — for tasks table
    ):
        self._librarian = librarian
        self._db_path = db_path
        self._refresh_seconds = refresh_seconds
        self._project_id = project_id
        self._storage = storage
        self._console = Console() if _RICH else None
        self._start_time = datetime.utcnow()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self) -> None:
        """Render a single snapshot of the dashboard to stdout."""
        if not _RICH or self._console is None:
            self._plain_render()
            return
        layout = self._build_layout()
        self._console.print(layout)

    def live_loop(self) -> None:
        """
        Auto-refresh the dashboard every `refresh_seconds` seconds.
        Runs until KeyboardInterrupt.
        """
        if not _RICH or self._console is None:
            logger.warning("Dashboard: rich not available — falling back to plain render")
            self.render()
            return

        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            import time
            try:
                while True:
                    time.sleep(self._refresh_seconds)
                    live.update(self._build_layout())
            except KeyboardInterrupt:
                pass

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> "Panel":
        """Compose the full dashboard as a single Rich renderable."""
        header = self._make_header()
        agents_panel = self._make_agents_panel()
        nodes_panel = self._make_nodes_panel()
        cache_panel = self._make_cache_panel()
        tasks_panel = self._make_tasks_panel()
        research_panel = self._make_research_panel()

        # Row 1: agents | cache
        row1 = Columns([agents_panel, cache_panel], padding=(0, 2), expand=True)
        # Row 2: tasks | research feed
        row2 = Columns([tasks_panel, research_panel], padding=(0, 2), expand=True)

        from rich.console import Group
        content = Group(header, row1, row2, nodes_panel)

        return Panel(
            content,
            title="[bold cyan]  ContextForge v3.0 — Omega-Pulse v2[/bold cyan]",
            subtitle=f"[dim]{datetime.utcnow().strftime('%H:%M:%S UTC')}[/dim]",
            border_style="bright_black",
            padding=(0, 1),
        )

    def _make_header(self) -> "Text":
        uptime = datetime.utcnow() - self._start_time
        secs = int(uptime.total_seconds())
        up_str = f"{secs // 3600:02d}h {(secs % 3600) // 60:02d}m {secs % 60:02d}s"
        online = sum(1 for a in _AGENT_REGISTRY.values() if a["status"] == "online")
        total = len(_AGENT_REGISTRY)

        txt = Text()
        txt.append("  Phase 1 Pipeline  ", style="bold white on dark_blue")
        txt.append("  ")
        txt.append(f"Agents {online}/{total} online", style="green" if online > 0 else "yellow")
        txt.append("  │  ")
        txt.append(f"Uptime {up_str}", style="dim")
        txt.append("\n")
        return txt

    def _make_agents_panel(self) -> "Panel":
        table = Table(
            show_header=True,
            header_style="bold dim",
            box=box.SIMPLE_HEAD,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("", width=3)    # emoji
        table.add_column("Agent", style="bold")
        table.add_column("Role", style="dim")
        table.add_column("Status", justify="right")

        status_styles = {
            "online":  ("🟢", "green"),
            "offline": ("🔴", "red"),
            "pending": ("⚪", "bright_black"),
        }

        for name, info in _AGENT_REGISTRY.items():
            st = info["status"]
            dot, color = status_styles.get(st, ("⚪", "dim"))
            table.add_row(
                info["emoji"],
                name,
                info["role"],
                Text(f"{dot} {st}", style=color),
            )

        return Panel(table, title="[bold]Agents[/bold]", border_style="bright_black", padding=(0, 1))

    def _make_cache_panel(self) -> "Panel":
        stats = self._get_cache_stats()

        table = Table(
            show_header=False,
            box=box.SIMPLE,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right", style="bold")

        table.add_row("L1 entries", str(stats["l1_entries"]))
        table.add_row("Reverse index", str(stats["reverse_index_entries"]))
        table.add_row("Total hits", Text(str(stats["total_cache_hits"]), style="green"))
        table.add_row("Max entries", str(stats["max_l1_entries"]))

        hit_rate_str = "—"
        total_hits = stats["total_cache_hits"]
        l1_entries = stats["l1_entries"]
        if l1_entries > 0:
            hit_pct = min(100.0, (total_hits / max(total_hits + l1_entries, 1)) * 100)
            color = "green" if hit_pct >= 60 else "yellow"
            hit_rate_str = Text(f"{hit_pct:.1f}%", style=color)
        table.add_row("Est. hit rate", hit_rate_str)

        # LLM model indicator
        table.add_row("", "")
        table.add_row("[dim]LLM path[/dim]", _current_llm_label())

        return Panel(table, title="[bold]Cache / LLM[/bold]", border_style="bright_black", padding=(0, 1))

    def _make_nodes_panel(self) -> "Panel":
        nodes = self._get_recent_nodes(limit=5)

        if not nodes:
            return Panel(
                Text("  No decision nodes yet — save a file to trigger the pipeline.", style="dim"),
                title="[bold]Recent Decision Nodes[/bold]",
                border_style="bright_black",
            )

        table = Table(
            show_header=True,
            header_style="bold dim",
            box=box.SIMPLE_HEAD,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("#", width=3, style="dim")
        table.add_column("Summary", ratio=4)
        table.add_column("Area", ratio=1, style="cyan")
        table.add_column("Conf", width=6, justify="right")
        table.add_column("Status", width=10, justify="right")
        table.add_column("Agent", width=12, style="dim")
        table.add_column("Saved", width=10, style="dim")

        status_colors = {
            "active":      "green",
            "pending":     "yellow",
            "deprecated":  "bright_black",
            "quarantined": "red",
        }

        for i, n in enumerate(nodes, 1):
            conf = float(n.get("confidence") or 0)
            conf_color = "green" if conf >= 0.85 else "yellow" if conf >= 0.50 else "red"
            st = n.get("status", "pending")
            ts_raw = n.get("created_at", "")
            ts = ts_raw[:16].replace("T", " ") if ts_raw else "—"
            summary = (n.get("summary") or "")[:55]
            if len(n.get("summary", "")) > 55:
                summary += "…"

            table.add_row(
                str(i),
                summary,
                n.get("area", "—"),
                Text(f"{conf:.2f}", style=conf_color),
                Text(st, style=status_colors.get(st, "dim")),
                n.get("created_by_agent", "—"),
                ts,
            )

        return Panel(
            table,
            title="[bold]Recent Decision Nodes[/bold]",
            border_style="bright_black",
            padding=(0, 1),
        )

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _get_cache_stats(self) -> dict:
        """Pull live stats from the Librarian if wired, else return zeros."""
        if self._librarian is not None:
            import asyncio
            try:
                from agentscope.message import Msg
                msg = Msg("Dashboard", content="stats", role="user", metadata={"action": "stats"})
                resp = asyncio.run(self._librarian.reply(msg))
                if resp.metadata:
                    return resp.metadata
            except Exception:
                pass
        return {
            "l1_entries": 0,
            "reverse_index_entries": 0,
            "total_cache_hits": 0,
            "max_l1_entries": 512,
        }

    def _get_recent_nodes(self, limit: int = 5) -> list[dict]:
        """Read the most recent nodes from SQLite, ordered by created_at DESC."""
        if not Path(self._db_path).exists():
            return []
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT id, summary, area, confidence, status, "
                "created_by_agent, created_at FROM decision_nodes "
                "WHERE tombstone=FALSE "
            )
            params: list = []
            if self._project_id:
                query += "AND project_id=? "
                params.append(self._project_id)
            query += "ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"Dashboard: could not read nodes — {exc}")
            return []

    # ------------------------------------------------------------------
    # Plain-text fallback
    # ------------------------------------------------------------------

    def _plain_render(self) -> None:
        """Minimal plain-text dashboard for environments without rich."""
        print("\n=== ContextForge v3.0 — Omega-Pulse ===")
        print(f"  Time: {datetime.utcnow().strftime('%H:%M:%S UTC')}")
        print("\n  Agents:")
        for name, info in _AGENT_REGISTRY.items():
            print(f"    {info['emoji']} {name:20s} {info['status']}")
        nodes = self._get_recent_nodes(5)
        print(f"\n  Recent nodes ({len(nodes)}):")
        for n in nodes:
            print(f"    [{n.get('status','?'):8s}] {n.get('summary','')[:60]}")
        print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_llm_label() -> str:
    """Return a short string describing the active LLM provider."""
    import os
    if os.getenv("GEMINI_API_KEY"):
        return Text("Gemini Flash ✓", style="green")
    if os.getenv("GROQ_API_KEY"):
        return Text("Groq ✓", style="green")
    try:
        import urllib.request
        urllib.request.urlopen(
            os.getenv("OLLAMA_URL", "http://localhost:11434"), timeout=0.3
        )
        return Text("Ollama ✓", style="green")
    except Exception:
        pass
    return Text("Fallback (no LLM)", style="yellow")
