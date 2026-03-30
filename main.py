"""
ContextForge v3.0 — Entry Point  (Phase 3: Builder Loop)
The Omega Persistence Engine

Phase 1 pipeline:
  Sentry → GhostCoder (LLM distiller) → HITL Gate → Librarian → SQLite

Phase 2 additions:
  PM Agent  — manages tasks table; breakdown of goals into sub-tasks
  Researcher — web search (Tavily→Serper→DuckDuckGo) → Knowledge Nodes
  Director loop — type @pm <goal> or @research <topic> in terminal

LLM priority (set keys in .env to activate):
  1. Gemini Flash   — GEMINI_API_KEY  (recommended: free tier, long context)
  2. Groq Llama     — GROQ_API_KEY    (alternative: free tier, very fast)
  3. Ollama local   — OLLAMA_URL      (local, zero cost, requires running server)
  4. Rule-based fallback (no key needed, always works)

Search priority (set keys in .env to activate):
  1. Tavily   — TAVILY_API_KEY  (best structured results)
  2. Serper   — SERPER_API_KEY  (Google results)
  3. DuckDuckGo — no key needed (always available)

Dashboard:
  An Omega-Pulse Rich terminal panel prints on startup and auto-refreshes
  every DASHBOARD_REFRESH seconds (default 10).

Run:
  python main.py
  python main.py --no-dashboard     # suppress the Rich panel
  python main.py --hitl-off         # auto-approve all nodes (batch testing)
  python main.py --no-director      # disable interactive Director loop

Director commands (type in terminal while running):
  @pm <goal>          Plan a goal into 3-5 tasks
  @research <topic>   Web-search and save a knowledge node
  @coder <task_id>    Execute a task: fetch context (RAG) → generate code → save node
  @historian gc       Run Historian garbage-collection (archive duplicates)
  @tasks              List current pending tasks
  @status             Show completion % and current sprint
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
import time

import agentscope
from dotenv import load_dotenv
from loguru import logger

from src.agents.coder import CoderAgent
from src.agents.ghost_coder import GhostCoderAgent
from src.agents.historian import HistorianAgent
from src.agents.librarian import LibrarianAgent
from src.agents.pm import PMAgent
from src.agents.researcher import ResearcherAgent
from src.agents.reviewer import ShadowReviewer
from src.agents.sentry import SentryAgent
from src.core.dashboard import OmegaDashboard, set_agent_online
from src.core.router import TokenRouter
from src.core.signals import SignalBatch
from src.core.storage import StorageAdapter
from src.skills.web_search import WebSearchSkill

load_dotenv()


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ContextForge v3.0")
    p.add_argument("--no-dashboard", action="store_true", help="Suppress Rich dashboard")
    p.add_argument("--hitl-off", action="store_true", help="Auto-approve all nodes")
    p.add_argument("--no-director", action="store_true", help="Disable interactive Director loop")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Model spec builders  (AgentScope 1.0.18 — direct instantiation)
# ---------------------------------------------------------------------------

def _gemini_spec() -> dict | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    return {
        "model_type": "gemini_chat",
        "model_name": "gemini-2.5-flash-preview-05-20",
        "api_key": key,
        "temperature": 0.2,
    }


def _groq_spec() -> dict | None:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    return {
        "model_type": "openai_chat",
        "model_name": "llama-3.3-70b-versatile",
        "api_key": key,
        "base_url": "https://api.groq.com/openai/v1",
        "temperature": 0.3,
    }


def _ollama_spec() -> dict:
    return {
        "model_type": "ollama_chat",
        "model_name": os.getenv("OLLAMA_MODEL", "llama3.3"),
        "base_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
    }


def _pick_model_spec() -> tuple[dict | None, str]:
    """
    Return (spec_dict, label) for the highest-priority available LLM.
    Gemini → Groq → Ollama → None (rule-based fallback).
    """
    for spec, label in [
        (_gemini_spec(), "Gemini Flash (free tier)"),
        (_groq_spec(),   "Groq / Llama-3.3-70B (free tier)"),
    ]:
        if spec:
            return spec, label
    return _ollama_spec(), "Ollama (local)"


def _make_model_fn_from_spec(model_spec: dict | None):
    """
    Build a sync callable(messages: list[dict]) → str from a model spec dict.
    Used to share the same LLM with PM, Researcher, and Coder.
    """
    if not model_spec:
        return None

    model_type = model_spec.get("model_type", "")
    spec = {k: v for k, v in model_spec.items() if k != "model_type"}

    try:
        if model_type == "ollama_chat":
            from agentscope.model import OllamaChatModel
            model = OllamaChatModel(
                model_name=spec.get("model_name", "llama3.3"),
                host=spec.get("base_url"),
            )
        elif model_type == "openai_chat":
            from agentscope.model import OpenAIChatModel
            model = OpenAIChatModel(
                model_name=spec.get("model_name", ""),
                api_key=spec.get("api_key", ""),
                base_url=spec.get("base_url"),
                temperature=spec.get("temperature", 0.3),
            )
        elif model_type == "gemini_chat":
            from agentscope.model import GeminiChatModel
            model = GeminiChatModel(
                model_name=spec.get("model_name", ""),
                api_key=spec.get("api_key", ""),
                temperature=spec.get("temperature", 0.2),
            )
        else:
            return None

        def _call(messages: list[dict]) -> str:
            response = asyncio.run(model(messages))
            texts = [
                block["text"]
                for block in response.content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(texts)

        return _call

    except Exception as exc:
        logger.warning(f"model_fn builder: could not bind ({exc}) — fallback")
        return None


def _build_token_router(cloud_spec: dict | None) -> TokenRouter:
    """
    Build a TokenRouter:
      < TOKEN_ROUTER_THRESHOLD tokens → Ollama (local)
      >= threshold                    → cloud_spec LLM (Gemini/Groq)

    If only one backend is available, it handles both tiers.
    """
    cloud_fn = _make_model_fn_from_spec(cloud_spec)
    local_fn = _make_model_fn_from_spec(_ollama_spec())
    return TokenRouter(local_fn=local_fn, cloud_fn=cloud_fn)


# ---------------------------------------------------------------------------
# Pipeline callback (Phase 1)
# ---------------------------------------------------------------------------

def _make_pipeline_callback(ghost_coder: GhostCoderAgent):
    def _on_batch(batch: SignalBatch) -> None:
        ghost_coder.process_batch(batch.model_dump())
    return _on_batch


# ---------------------------------------------------------------------------
# Director loop (Phase 2) — runs in a background thread
# ---------------------------------------------------------------------------

def _director_loop(pm: PMAgent, researcher: ResearcherAgent, coder: "CoderAgent", historian: "HistorianAgent") -> None:
    """
    Interactive command loop.  Parses @pm / @research / @coder commands from stdin.
    Runs in a daemon thread so it doesn't block the main observation loop.
    """
    print("\n  Director online. Commands: @pm <goal>  |  @research <topic>  |  @coder <task_id>  |  @historian gc  |  @tasks  |  @status  |  quit\n")

    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            print("  Director: bye.")
            os.kill(os.getpid(), signal.SIGTERM)
            break

        if line.startswith("@pm "):
            goal = line[4:].strip()
            if not goal:
                print("  Usage: @pm <goal description>")
                continue
            sprint = os.getenv("CURRENT_SPRINT", "Phase 2 Sprint 1")
            print(f"  PM: planning '{goal[:60]}' ...")
            from agentscope.message import Msg
            result = asyncio.run(pm.reply(
                Msg("Director", content="plan_goal",
                    role="user",
                    metadata={"action": "plan_goal", "goal": goal, "sprint": sprint})
            ))
            tasks = (result.metadata or {}).get("tasks", [])
            print(f"  PM: created {len(tasks)} task(s):")
            for t in tasks:
                print(f"    [{t.get('priority',3)}] {t.get('title','')[:60]} → {t.get('assigned_to','?')}")

            # Auto-trigger research for Researcher-assigned tasks
            research_tasks = [t for t in tasks if t.get("assigned_to") == "Researcher"]
            for rt in research_tasks[:1]:  # limit to 1 auto-search per command
                q = rt.get("description") or rt.get("title", "")
                if q:
                    print(f"  Researcher: auto-searching '{q[:50]}' ...")
                    r = researcher.research(q)
                    node = r.get("node", {})
                    print(f"  Researcher: node {node.get('id','?')[:8]} saved — conf={node.get('confidence',0):.2f}")

        elif line.startswith("@research "):
            query = line[10:].strip()
            if not query:
                print("  Usage: @research <topic>")
                continue
            print(f"  Researcher: searching '{query[:60]}' ...")
            r = researcher.research(query)
            node = r.get("node", {})
            links = node.get("type_metadata", {}).get("key_links", [])
            print(f"  Researcher: {r.get('action','?')} — conf={node.get('confidence',0):.2f}")
            for lnk in links[:2]:
                print(f"    {lnk}")

        elif line.startswith("@coder "):
            task_id = line[7:].strip()
            if not task_id:
                print("  Usage: @coder <task_id>  (prefix match supported)")
                continue
            print(f"  Coder: fetching context and executing task '{task_id[:16]}' ...")
            result = coder.execute(task_id)
            if result.get("action") == "error":
                print(f"  Coder ERROR: {result.get('detail')}")
                continue
            print(f"  Coder: done — RAG tier={result.get('rag_tier')} "
                  f"conf={result.get('confidence',0):.2f} "
                  f"node={result.get('node_id','dry-run')[:8] if result.get('node_id') else 'dry-run'}")
            print(f"\n  --- Plan ---")
            for step in result.get("plan", []):
                print(f"  {step}")
            print(f"\n  --- Code Preview ---")
            code = result.get("code_block", "")
            for line_ in code.splitlines()[:20]:
                print(f"  {line_}")
            if len(code.splitlines()) > 20:
                print(f"  ... ({len(code.splitlines())-20} more lines)")
            print()

        elif line.startswith("@historian"):
            cmd = line[10:].strip()
            if cmd == "gc" or cmd == "":
                print("  Historian: running GC (archive duplicates) ...")
                result = historian.run_gc()
                print(
                    f"  Historian: groups={result.get('groups_found',0)} "
                    f"archived={result.get('archived',0)}"
                )
            else:
                print("  Usage: @historian gc")

        elif line.strip() == "@tasks":
            from agentscope.message import Msg
            result = asyncio.run(pm.reply(
                Msg("Director", content="list_tasks", role="user",
                    metadata={"action": "list_tasks", "status": "pending"})
            ))
            tasks = (result.metadata or {}).get("tasks", [])
            if not tasks:
                print("  No pending tasks.")
            for t in tasks:
                print(f"  [{t.get('status','?'):11s}] P{t.get('priority',3)} {t.get('title','')[:55]}")

        elif line.strip() == "@status":
            from agentscope.message import Msg
            result = asyncio.run(pm.reply(
                Msg("Director", content="get_stats", role="user",
                    metadata={"action": "get_stats"})
            ))
            meta = result.metadata or {}
            print(
                f"  Completion: {meta.get('pct_complete',0)}%  "
                f"done={meta.get('done',0)}  pending={meta.get('pending',0)}  "
                f"in_progress={meta.get('in_progress',0)}  "
                f"sprint={meta.get('current_sprint') or 'N/A'}"
            )

        else:
            print("  Unknown command. Try: @pm <goal> | @research <topic> | @tasks | @status | quit")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    if args.hitl_off:
        os.environ["HITL_AUTO_APPROVE"] = "true"

    logger.info("ContextForge v3.0 — starting Phase 2 pipeline")

    # ── AgentScope init ───────────────────────────────────────────────
    agentscope.init(project="ContextForge", logging_level="INFO")

    # ── Shared StorageAdapter ─────────────────────────────────────────
    db_path = os.getenv("DB_PATH", "data/contextforge.db")
    storage = StorageAdapter(db_path=db_path)

    # ── Librarian ─────────────────────────────────────────────────────
    librarian = LibrarianAgent(name="Librarian", db_path=db_path)
    set_agent_online("Librarian")
    logger.info(f"Librarian online — db={db_path}")

    # ── LLM model fn (shared by GhostCoder, PM, Researcher, Coder) ───
    model_spec, model_label = _pick_model_spec()
    shared_model_fn = _make_model_fn_from_spec(model_spec)

    # ── TokenRouter (Phase 4) ─────────────────────────────────────────
    # Routes: <500 tokens → Ollama local, >=500 → cloud (Gemini/Groq)
    token_router = _build_token_router(model_spec)
    router_label = token_router.describe()

    # ── Ghost-Coder ───────────────────────────────────────────────────
    ghost_coder = GhostCoderAgent(
        name="GhostCoder",
        model_spec=model_spec,
        project_type=os.getenv("PROJECT_TYPE", "code"),
        project_id=os.getenv("PROJECT_ID"),
        librarian=librarian,
    )
    set_agent_online("GhostCoder")
    logger.info(f"GhostCoder online — LLM: {model_label}")

    # ── PM Agent ──────────────────────────────────────────────────────
    pm = PMAgent(
        name="PM",
        model_fn=shared_model_fn,
        storage=storage,
        project_id=os.getenv("PROJECT_ID"),
    )
    set_agent_online("PM")
    logger.info("PM online")

    # ── Researcher ────────────────────────────────────────────────────
    search_skill = WebSearchSkill(max_results=5)
    researcher = ResearcherAgent(
        name="Researcher",
        model_fn=shared_model_fn,
        search_skill=search_skill,
        librarian=librarian,
        project_id=os.getenv("PROJECT_ID"),
    )
    set_agent_online("Researcher")
    logger.info(f"Researcher online — search backend: {search_skill.backend}")

    # ── Shadow-Reviewer ───────────────────────────────────────────────
    reviewer = ShadowReviewer(
        name="Shadow-Reviewer",
        storage=storage,
        project_id=os.getenv("PROJECT_ID"),
    )
    set_agent_online("Shadow-Reviewer")
    logger.info("Shadow-Reviewer online")

    # ── Historian ─────────────────────────────────────────────────────
    historian = HistorianAgent(
        name="Historian",
        storage=storage,
        project_id=os.getenv("PROJECT_ID"),
    )
    set_agent_online("Historian")
    logger.info("Historian online")

    # ── Coder (now with Reviewer gate) ────────────────────────────────
    coder = CoderAgent(
        name="Coder",
        model_fn=shared_model_fn,
        librarian=librarian,
        storage=storage,
        project_id=os.getenv("PROJECT_ID"),
        reviewer=reviewer,
    )
    set_agent_online("Coder")
    logger.info("Coder online")

    # ── Sentry ────────────────────────────────────────────────────────
    sentry = SentryAgent(
        name="Sentry",
        watch_path=os.getenv("SENTRY_WATCH_PATH", "."),
        batch_threshold=int(os.getenv("SENTRY_BATCH_THRESHOLD", "3")),
        pause_timeout=int(os.getenv("SENTRY_PAUSE_TIMEOUT", "30")),
        project_id=os.getenv("PROJECT_ID"),
    )
    pipeline_cb = _make_pipeline_callback(ghost_coder)
    _orig = sentry._broadcast

    def _patched(batch: SignalBatch) -> None:
        _orig(batch)
        pipeline_cb(batch)

    sentry._broadcast = _patched  # type: ignore[method-assign]
    sentry.start_watching()
    set_agent_online("Sentry")
    logger.info(
        "Sentry online — watching: "
        + os.path.abspath(os.getenv("SENTRY_WATCH_PATH", "."))
    )

    # ── Dashboard ─────────────────────────────────────────────────────
    dashboard = OmegaDashboard(
        librarian=librarian,
        db_path=db_path,
        refresh_seconds=int(os.getenv("DASHBOARD_REFRESH", "10")),
        project_id=os.getenv("PROJECT_ID"),
        storage=storage,
    )

    # ── Graceful shutdown ─────────────────────────────────────────────
    def _shutdown(sig: int, frame) -> None:  # type: ignore[type-arg]
        logger.info("ContextForge: shutdown signal received")
        sentry.stop_watching()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Banner ────────────────────────────────────────────────────────
    hitl_threshold = float(os.getenv("HITL_CONFIDENCE_THRESHOLD", "0.70"))
    hitl_auto = os.getenv("HITL_AUTO_APPROVE", "false").lower() == "true"

    logger.info(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  ContextForge v3.0 — Phase 4: Hardened\n"
        "  Sentry  GhostCoder  Librarian  HITL\n"
        "  PM  Researcher  Coder  Reviewer  Historian\n"
        f"  LLM    : {model_label}\n"
        f"  Router : {router_label}\n"
        f"  Search : {search_skill.backend}\n"
        f"  DB     : {db_path}\n"
        f"  HITL   : threshold={hitl_threshold}  auto={hitl_auto}\n"
        "  Save a .py/.md/.json file  OR  type @pm/@research/@coder commands.\n"
        "  Press Ctrl+C to stop.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # ── Initial dashboard render ──────────────────────────────────────
    if not args.no_dashboard:
        dashboard.render()

    # ── Director loop in background thread ───────────────────────────
    if not args.no_director and sys.stdin.isatty():
        director_thread = threading.Thread(
            target=_director_loop,
            args=(pm, researcher, coder, historian),
            daemon=True,
            name="Director",
        )
        director_thread.start()

    # ── Observation loop ──────────────────────────────────────────────
    refresh = int(os.getenv("DASHBOARD_REFRESH", "10"))
    last_refresh = time.monotonic()

    try:
        while True:
            time.sleep(1)
            if not args.no_dashboard:
                now = time.monotonic()
                if now - last_refresh >= refresh:
                    dashboard.render()
                    last_refresh = now
    except KeyboardInterrupt:
        _shutdown(0, None)


if __name__ == "__main__":
    main()
