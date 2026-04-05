"""
ContextForge v3.0 — Quickstart / Hello World

Demonstrates:
  1. Zero-config engine boot  (ContextForge.init)
  2. Multi-agent task pipeline (PM → Researcher → Coder)
  3. Shadow-Reviewer middleware blocking a destructive command
  4. Historian GC cleaning a duplicate knowledge node

Run:
    python quickstart.py

No API key required — the rule-based fallback activates automatically.
Add GEMINI_API_KEY or GROQ_API_KEY to .env for full LLM power.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from rich import print as rprint
from rich.panel import Panel
from rich.table import Table
from rich import box

load_dotenv()

# ── Zero-config boot ────────────────────────────────────────────────────────

os.environ.setdefault("PROJECT_ID", "quickstart-demo")
os.environ.setdefault("DB_PATH", "data/quickstart.db")
os.environ["HITL_AUTO_APPROVE"] = "true"   # no interactive prompts in demo

from src.engine import ContextForge

rprint(Panel.fit(
    "[bold cyan]ContextForge v3.0[/bold cyan] — Quickstart Demo\n"
    "[dim]The Omega Persistence Engine[/dim]",
    border_style="cyan",
))


# ── Step 1: Boot the engine ─────────────────────────────────────────────────

rprint("\n[bold]Step 1[/bold] — Booting engine (auto-detects LLM from .env) ...")
cf = ContextForge.init(
    project_id="quickstart-demo",
    db_path="data/quickstart.db",
    watch_path=".",
    hitl_auto=True,
    dashboard=False,
)

rprint(f"  [green]✓[/green] Engine online — LLM: [yellow]{cf.model_label}[/yellow]")
rprint(f"  [green]✓[/green] Agents: {', '.join(cf.agents.keys())}")


# ── Step 2: Multi-agent task (PM → Researcher → Coder) ─────────────────────

rprint("\n[bold]Step 2[/bold] — Running multi-agent goal: 'Build a Python web scraper'")

result = cf.run("Build a Python web scraper that extracts headlines from news sites")

tasks = result.get("tasks", [])
executed = result.get("executed", [])

t = Table(title="PM Tasks Created", box=box.SIMPLE, show_header=True)
t.add_column("Priority", style="cyan", width=8)
t.add_column("Title", style="white")
t.add_column("Assigned To", style="yellow")
for task in tasks:
    t.add_row(
        str(task.get("priority", 3)),
        task.get("title", "")[:55],
        task.get("assigned_to", "Coder"),
    )
rprint(t)

if executed:
    ex = executed[0]
    rprint(
        f"  [green]✓[/green] Coder executed: [italic]{ex['task']}[/italic]\n"
        f"     Verdict: [bold {'green' if ex['verdict']=='APPROVED' else 'yellow'}]{ex['verdict']}[/bold {'green' if ex['verdict']=='APPROVED' else 'yellow'}]  "
        f"RAG tier: {ex['rag_tier']}  "
        f"Node: {ex['node_id'] or 'dry-run'}"
    )


# ── Step 3: Shadow-Reviewer blocks destructive command ─────────────────────

rprint("\n[bold]Step 3[/bold] — Defense test: feeding the Coder a destructive task ...")

# Seed an active Sentry node so the Reviewer has something to protect
sentry_node = {
    "id": str(uuid.uuid4()),
    "project_id": "quickstart-demo",
    "summary": "Sentry Agent active — file watcher monitoring project directory",
    "rationale": "Sentry monitors file changes and feeds signals to GhostCoder pipeline",
    "area": "sentry",
    "alternatives": [], "dependencies": [],
    "triggered_by": "quickstart-seed", "confidence": 0.95, "importance": 0.9,
    "vclock": {}, "origin_client": "quickstart", "tombstone": False,
    "created_by_agent": "Quickstart", "validated_by": "", "audited_by": "",
    "status": "active", "type_metadata": {},
    "created_at": datetime.utcnow().isoformat(),
    "updated_at": datetime.utcnow().isoformat(),
}
cf._s.storage.upsert_node(sentry_node)

evil_task = {
    "id": str(uuid.uuid4()),
    "project_id": "quickstart-demo",
    "title": "Delete the Sentry Agent",
    "description": "Remove and delete the Sentry file watcher from the codebase",
    "status": "pending", "priority": 1, "sprint": "Demo",
    "assigned_to": "Coder",
    "created_at": datetime.utcnow().isoformat(),
    "updated_at": datetime.utcnow().isoformat(),
}
cf._s.storage.upsert_task(evil_task)

block_result = cf.execute_task(evil_task["id"])
verdict = block_result.get("verdict", "UNKNOWN")
detail = block_result.get("contradiction_detail", "")

color = "green" if verdict == "BLOCKED" else "red"
icon  = "✓" if verdict == "BLOCKED" else "✗"

rprint(
    f"  [{color}]{icon}[/{color}] Reviewer verdict: [bold {color}]{verdict}[/bold {color}]\n"
    f"     Conflicting entity: [italic]{detail}[/italic]\n"
    f"     Sentry is [bold green]protected[/bold green] — task rejected."
    if verdict == "BLOCKED" else
    f"  [yellow]![/yellow] Expected BLOCKED but got {verdict} — check reviewer wiring."
)


# ── Step 4: Historian GC ────────────────────────────────────────────────────

rprint("\n[bold]Step 4[/bold] — Historian GC: seeding a duplicate node and archiving ...")

dup_node = dict(
    sentry_node,
    id=str(uuid.uuid4()),
    created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00",
)
cf._s.storage.upsert_node(dup_node)
rprint(f"  Seeded duplicate node: {dup_node['id'][:8]}")

gc = cf.historian_gc()
rprint(
    f"  [green]✓[/green] Historian GC complete:\n"
    f"     Duplicate groups found : {gc.get('groups_found', 0)}\n"
    f"     Nodes archived         : {gc.get('archived', 0)}\n"
    f"     Archived IDs           : {[i[:8] for i in gc.get('archived_ids', [])]}"
)


# ── Step 5: Status summary ──────────────────────────────────────────────────

rprint("\n[bold]Step 5[/bold] — Project status ...")
stats = cf.status()
rprint(
    f"  Completion : [bold]{stats.get('pct_complete', 0)}%[/bold]  "
    f"Done: {stats.get('done', 0)}  "
    f"Pending: {stats.get('pending', 0)}  "
    f"In-progress: {stats.get('in_progress', 0)}"
)


# ── Shutdown ─────────────────────────────────────────────────────────────────

cf.shutdown()

rprint(Panel.fit(
    "[bold green]Quickstart complete![/bold green]\n\n"
    "Next steps:\n"
    "  [cyan]python main.py[/cyan]              — full interactive Director loop\n"
    "  [cyan]python main.py --hitl-off[/cyan]   — auto-approve all nodes\n"
    "  [cyan]python main.py --no-dashboard[/cyan] — suppress Rich panel\n\n"
    "  Set [yellow]GEMINI_API_KEY[/yellow] or [yellow]GROQ_API_KEY[/yellow] in [italic].env[/italic] to activate cloud LLMs.",
    border_style="green",
    title="ContextForge v3.0",
))
