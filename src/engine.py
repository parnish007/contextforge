"""
ContextForge v3.0 — Zero-Config Engine
The Omega Persistence Engine

`ContextForge.init()` boots the full 8-agent stack from a single call.
No manual wiring, no boilerplate — just bring an API key (or run Ollama).

Usage
-----
    from src.engine import ContextForge

    cf = ContextForge.init()          # auto-detects LLM, watches cwd
    cf.run("Build a Python scraper")  # PM → Researcher → Coder pipeline
    cf.historian_gc()                 # archive duplicate knowledge nodes
    cf.shutdown()

Environment Variables (all optional — safe defaults built in)
--------------------------------------------------------------
    GEMINI_API_KEY   — activates Gemini Flash (recommended free-tier cloud LLM)
    GROQ_API_KEY     — activates Groq / Llama-3.3-70B (fast free-tier cloud LLM)
    OLLAMA_URL       — Ollama base URL (default http://localhost:11434)
    OLLAMA_MODEL     — Ollama model name (default llama3.3)
    PROJECT_ID       — knowledge graph project namespace (default contextforge-default)
    DB_PATH          — SQLite database path (default data/contextforge.db)
    SENTRY_WATCH_PATH — directory for Sentry to watch (default current dir)
    TOKEN_ROUTER_THRESHOLD — token boundary local↔cloud (default 500)
    HITL_AUTO_APPROVE     — set "true" to skip human review prompts
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from typing import Callable

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

ModelFn = Callable[[list[dict]], str]


# ---------------------------------------------------------------------------
# LLM spec helpers
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
    for spec, label in [
        (_gemini_spec(), "Gemini Flash"),
        (_groq_spec(), "Groq / Llama-3.3-70B"),
    ]:
        if spec:
            return spec, label
    return _ollama_spec(), "Ollama (local)"


def _make_model_fn(model_spec: dict | None) -> ModelFn | None:
    """
    Compile a model spec dict into a sync callable(messages) → str.
    For Gemini, routes through the direct google-genai adapter (bypasses
    the broken AgentScope GeminiChatModel wrapper).
    """
    if not model_spec:
        return None

    # Fast-path: Gemini → use direct adapter that handles the new SDK format
    if model_spec.get("model_type") == "gemini_chat":
        from src.skills.gemini_direct import make_gemini_fn
        fn = make_gemini_fn(
            api_key=model_spec.get("api_key"),
            model=None,          # auto-select from preference list
            temperature=model_spec.get("temperature", 0.2),
        )
        if fn:
            return fn
        logger.warning("Gemini direct adapter unavailable — falling through to rule-based fallback")
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
        logger.warning(f"ContextForge: could not bind model ({exc}) — stub fallback active")
        return None


# ---------------------------------------------------------------------------
# EngineState — everything the running engine owns
# ---------------------------------------------------------------------------

@dataclass
class EngineState:
    storage: StorageAdapter
    librarian: LibrarianAgent
    ghost_coder: GhostCoderAgent
    pm: PMAgent
    researcher: ResearcherAgent
    reviewer: ShadowReviewer
    historian: HistorianAgent
    coder: CoderAgent
    sentry: SentryAgent
    router: TokenRouter
    dashboard: OmegaDashboard
    model_label: str
    project_id: str
    db_path: str
    _active: bool = field(default=True, repr=False)


# ---------------------------------------------------------------------------
# ContextForge — public API
# ---------------------------------------------------------------------------

class ContextForge:
    """
    Zero-config facade for the full ContextForge agent stack.

    Class methods
    -------------
    ContextForge.init(...)  →  ContextForge instance

    Instance methods
    ----------------
    run(goal)           →  dict  Plan a goal with PM, auto-research, then execute first task
    research(topic)     →  dict  Web-search + save Knowledge Node
    execute_task(id)    →  dict  Coder: fetch context (RAG) → generate code → save node
    historian_gc()      →  dict  Archive duplicate knowledge nodes
    status()            →  dict  Completion stats for current project
    render_dashboard()  →  None  Print Rich terminal panel
    shutdown()          →  None  Stop Sentry, clean up
    """

    def __init__(self, state: EngineState) -> None:
        self._s = state

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def init(
        cls,
        project_id: str | None = None,
        db_path: str | None = None,
        watch_path: str | None = None,
        hitl_auto: bool | None = None,
        dashboard: bool = False,
    ) -> "ContextForge":
        """
        Boot the full 8-agent stack.

        Parameters
        ----------
        project_id : str | None
            Namespace for the knowledge graph. Defaults to $PROJECT_ID env var
            or 'contextforge-default'.
        db_path : str | None
            Path to the SQLite database file. Defaults to $DB_PATH or
            'data/contextforge.db'.
        watch_path : str | None
            Directory for Sentry to monitor. Defaults to $SENTRY_WATCH_PATH
            or the current working directory.
        hitl_auto : bool | None
            If True, all HITL gates auto-approve (no terminal prompts).
            Defaults to the HITL_AUTO_APPROVE env var.
        dashboard : bool
            Render the Rich Omega-Pulse dashboard panel on startup.
        """
        # ── Resolve config ────────────────────────────────────────────
        pid = project_id or os.getenv("PROJECT_ID", "contextforge-default")
        dbp = db_path or os.getenv("DB_PATH", "data/contextforge.db")
        wpath = watch_path or os.getenv("SENTRY_WATCH_PATH", ".")

        if hitl_auto is True:
            os.environ["HITL_AUTO_APPROVE"] = "true"
        elif hitl_auto is False:
            os.environ.pop("HITL_AUTO_APPROVE", None)

        # ── AgentScope init ───────────────────────────────────────────
        agentscope.init(project="ContextForge", logging_level="INFO")

        # ── LLM selection ─────────────────────────────────────────────
        model_spec, model_label = _pick_model_spec()
        shared_fn = _make_model_fn(model_spec)

        # ── Hardware-aware TokenRouter ────────────────────────────────
        # Automatically routes by prompt length: short → Ollama, long → cloud
        local_fn = _make_model_fn(_ollama_spec())
        cloud_fn = _make_model_fn(model_spec) if model_spec else None
        router = TokenRouter(local_fn=local_fn, cloud_fn=cloud_fn)

        # ── Core storage ──────────────────────────────────────────────
        storage = StorageAdapter(db_path=dbp)

        # ── Librarian + auto-linked RAG layers ───────────────────────
        # L1 (volatile exact cache) is internal to LibrarianAgent.
        # L2 (BM25 SQLite) and L3 (research nodes) are auto-connected
        # via ContextRAG which reads from the same StorageAdapter.
        librarian = LibrarianAgent(name="Librarian", db_path=dbp)
        set_agent_online("Librarian")

        # ── GhostCoder ────────────────────────────────────────────────
        ghost_coder = GhostCoderAgent(
            name="GhostCoder",
            model_spec=model_spec,
            project_type=os.getenv("PROJECT_TYPE", "code"),
            project_id=pid,
            librarian=librarian,
        )
        set_agent_online("GhostCoder")

        # ── PM ────────────────────────────────────────────────────────
        pm = PMAgent(name="PM", model_fn=shared_fn, storage=storage, project_id=pid)
        set_agent_online("PM")

        # ── Researcher ────────────────────────────────────────────────
        search_skill = WebSearchSkill(max_results=5)
        researcher = ResearcherAgent(
            name="Researcher",
            model_fn=shared_fn,
            search_skill=search_skill,
            librarian=librarian,
            project_id=pid,
        )
        set_agent_online("Researcher")

        # ── Shadow-Reviewer middleware ────────────────────────────────
        # Wraps the Coder execution loop: every node candidate is vetted
        # for semantic match (≥0.80) and contradiction before Librarian write.
        reviewer = ShadowReviewer(name="Shadow-Reviewer", storage=storage, project_id=pid)
        set_agent_online("Shadow-Reviewer")

        # ── Historian ─────────────────────────────────────────────────
        historian = HistorianAgent(name="Historian", storage=storage, project_id=pid)
        set_agent_online("Historian")

        # ── Coder (Reviewer already wired as middleware) ──────────────
        coder = CoderAgent(
            name="Coder",
            model_fn=shared_fn,
            librarian=librarian,
            storage=storage,
            project_id=pid,
            reviewer=reviewer,
        )
        set_agent_online("Coder")

        # ── Sentry (file watcher → GhostCoder pipeline) ──────────────
        sentry = SentryAgent(
            name="Sentry",
            watch_path=wpath,
            batch_threshold=int(os.getenv("SENTRY_BATCH_THRESHOLD", "3")),
            pause_timeout=int(os.getenv("SENTRY_PAUSE_TIMEOUT", "30")),
            project_id=pid,
        )
        _orig = sentry._broadcast

        def _patched_broadcast(batch: SignalBatch) -> None:
            _orig(batch)
            ghost_coder.process_batch(batch.model_dump())

        sentry._broadcast = _patched_broadcast  # type: ignore[method-assign]
        sentry.start_watching()
        set_agent_online("Sentry")

        # ── Dashboard ─────────────────────────────────────────────────
        dash = OmegaDashboard(
            librarian=librarian,
            db_path=dbp,
            refresh_seconds=int(os.getenv("DASHBOARD_REFRESH", "10")),
            project_id=pid,
            storage=storage,
        )

        state = EngineState(
            storage=storage,
            librarian=librarian,
            ghost_coder=ghost_coder,
            pm=pm,
            researcher=researcher,
            reviewer=reviewer,
            historian=historian,
            coder=coder,
            sentry=sentry,
            router=router,
            dashboard=dash,
            model_label=model_label,
            project_id=pid,
            db_path=dbp,
        )

        engine = cls(state)

        logger.info(
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  ContextForge v3.0 — Omega Engine ONLINE\n"
            f"  LLM    : {model_label}\n"
            f"  Router : {router.describe()}\n"
            f"  Search : {search_skill.backend}\n"
            f"  DB     : {dbp}\n"
            f"  Watch  : {os.path.abspath(wpath)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        if dashboard:
            dash.render()

        return engine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, goal: str, sprint: str | None = None) -> dict:
        """
        High-level pipeline: PM decomposes the goal → Researcher fetches
        context for research tasks → Coder executes the first pending task.

        Returns a summary dict with keys: tasks, researched, executed.
        """
        s = self._s
        sp = sprint or os.getenv("CURRENT_SPRINT", "Sprint 1")
        from agentscope.message import Msg

        # Step 1: PM breaks goal into tasks
        result = asyncio.run(s.pm.reply(
            Msg("Engine", content="plan_goal", role="user",
                metadata={"action": "plan_goal", "goal": goal, "sprint": sp})
        ))
        tasks = (result.metadata or {}).get("tasks", [])
        logger.info(f"Engine.run: PM created {len(tasks)} task(s) for '{goal[:60]}'")

        # Step 2: Auto-research for Researcher-assigned tasks
        researched = []
        for t in tasks:
            if t.get("assigned_to") == "Researcher":
                q = t.get("description") or t.get("title", "")
                if q:
                    r = s.researcher.research(q)
                    researched.append(r.get("node", {}).get("id", "")[:8])

        # Step 3: Execute first Coder task
        executed = []
        for t in tasks:
            if t.get("assigned_to") in ("Coder", None, ""):
                res = s.coder.execute(t["id"])
                executed.append({
                    "task": t.get("title", "")[:60],
                    "verdict": res.get("verdict"),
                    "rag_tier": res.get("rag_tier"),
                    "node_id": (res.get("node_id") or "")[:8],
                })
                break  # one task per run() call

        return {"tasks": tasks, "researched": researched, "executed": executed}

    def research(self, topic: str) -> dict:
        """Web-search `topic` and save a Knowledge Node. Returns node dict."""
        result = self._s.researcher.research(topic)
        logger.info(f"Engine.research: '{topic[:60]}' → node {result.get('node', {}).get('id', '?')[:8]}")
        return result

    def execute_task(self, task_id: str) -> dict:
        """
        Run one task through the full RAG → Plan-and-Execute → Reviewer pipeline.
        `task_id` supports prefix matching.
        """
        result = self._s.coder.execute(task_id)
        logger.info(
            f"Engine.execute_task: verdict={result.get('verdict')} "
            f"tier={result.get('rag_tier')} node={result.get('node_id', '')[:8]}"
        )
        return result

    def historian_gc(self) -> dict:
        """Archive duplicate knowledge nodes. Returns GC stats."""
        result = self._s.historian.run_gc()
        logger.info(
            f"Engine.historian_gc: groups={result.get('groups_found', 0)} "
            f"archived={result.get('archived', 0)}"
        )
        return result

    def status(self) -> dict:
        """Return PM task completion stats for the current project."""
        from agentscope.message import Msg
        result = asyncio.run(self._s.pm.reply(
            Msg("Engine", content="get_stats", role="user",
                metadata={"action": "get_stats"})
        ))
        return result.metadata or {}

    def render_dashboard(self) -> None:
        """Print the Rich Omega-Pulse terminal panel."""
        self._s.dashboard.render()

    def shutdown(self) -> None:
        """Stop the Sentry file watcher and mark engine inactive."""
        self._s.sentry.stop_watching()
        self._s._active = False
        logger.info("ContextForge engine shutdown.")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def project_id(self) -> str:
        return self._s.project_id

    @property
    def db_path(self) -> str:
        return self._s.db_path

    @property
    def model_label(self) -> str:
        return self._s.model_label

    @property
    def agents(self) -> dict:
        """Return all agent instances keyed by name."""
        s = self._s
        return {
            "Sentry": s.sentry,
            "GhostCoder": s.ghost_coder,
            "Librarian": s.librarian,
            "PM": s.pm,
            "Researcher": s.researcher,
            "Shadow-Reviewer": s.reviewer,
            "Historian": s.historian,
            "Coder": s.coder,
        }
