"""
ContextForge Nexus Architecture — Universal Hub Connector
==================================================

Exposes the Nexus Event-Sourced Memory as a queryable Context-API so that
external agents (Job Agent, Scene Sorter, any MCP-compatible client) can
retrieve structured context without replaying the full event ledger.

Architecture
────────────

  ┌─────────────────────────────────────────────────────────┐
  │              Universal Hub Connector                    │
  │                                                         │
  │  ContextAPI (HTTP/FastAPI)                              │
  │    GET  /context/query?q=<topic>&top_k=5               │
  │    GET  /context/history?n=20&agent=<name>             │
  │    GET  /context/entity/<entity_name>                  │
  │    POST /context/export  → JSON snapshot of memory     │
  │    GET  /context/health                                 │
  │                                                         │
  │  MemoryBus (in-process or cross-process via asyncio)   │
  │    publish(event_type, content)                         │
  │    subscribe(callback, filter_types=[...])             │
  │                                                         │
  │  ExternalAgentAdapter                                   │
  │    register(agent_id, description, query_fn)           │
  │    dispatch(query, agent_id=None)   → best-match agent │
  └─────────────────────────────────────────────────────────┘
          │                        │
          ▼                        ▼
  EventLedger (SQLite)    LocalIndexer (embeddings)
  JITLibrarian (cache)    StorageAdapter (graph nodes)

Usage — Embedded
────────────────
  from src.bridge.hub_connector import HubConnector

  hub = HubConnector()
  hub.start()                                    # background HTTP on port 9000

  # Query from any external agent
  result = await hub.query("What are the user's project goals?")
  result = await hub.get_entity("JWT service")
  result = await hub.get_history(n=10, agent="coder")

Usage — HTTP (curl)
───────────────────
  curl "http://localhost:9000/context/query?q=project+goals&top_k=5"
  curl "http://localhost:9000/context/entity/JWT%20service"
  curl "http://localhost:9000/context/history?n=10"
  curl -X POST "http://localhost:9000/context/export"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.memory.ledger import EventLedger, EventType
from src.core.storage import StorageAdapter
from src.retrieval.local_indexer import LocalIndexer

# Optional FastAPI — graceful degradation
try:
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.responses import JSONResponse
    import uvicorn
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


# ── Permission constants ──────────────────────────────────────────────────────

# Event types that are ALWAYS blocked for external agents regardless of policy.
# These contain system-integrity data that must never leave the Nexus boundary.
# Stored in LOWERCASE so that comparisons normalised via .lower() are consistent
# with raw ledger values (EventType enum uses UPPERCASE; all comparisons must
# normalise to the same case before checking membership).
_SYSTEM_PROTECTED_TYPES: frozenset[str] = frozenset({
    EventType.CONFLICT.value.lower(),     # "conflict"  — charter violation records
    EventType.ROLLBACK.value.lower(),     # "rollback"  — rollback audit entries
    EventType.CHECKPOINT.value.lower(),   # "checkpoint" — internal snapshot markers
})

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PermissionPolicy:
    """
    Access-control policy for a registered external agent.

    allowed_event_types : if non-empty, ONLY these event types are visible.
    blocked_event_types : these event types are always hidden (additive deny).

    The system-protected types (CONFLICT, ROLLBACK, CHECKPOINT) are blocked
    for ALL external agents and cannot be overridden via ``allowed_event_types``.

    Example — Job Agent policy::

        PermissionPolicy(
            allowed_event_types=["user_input", "research", "node_approved"],
            blocked_event_types=["agent_thought", "file_diff"],
        )
    """
    allowed_event_types: list[str] = field(default_factory=list)
    blocked_event_types: list[str] = field(default_factory=list)

    def is_permitted(self, event_type: str) -> bool:
        """Return True if *event_type* is visible under this policy."""
        et = event_type.lower()
        # System-protected types are unconditionally denied
        if et in _SYSTEM_PROTECTED_TYPES:
            return False
        # Explicit deny list
        if et in {b.lower() for b in self.blocked_event_types}:
            return False
        # Allow-list enforcement (empty allow-list = allow all non-blocked)
        if self.allowed_event_types:
            return et in {a.lower() for a in self.allowed_event_types}
        return True


@dataclass
class ContextResult:
    """A single context item returned by the Context-API."""
    source:     str              # "ledger" | "graph" | "local_index"
    event_type: str              # EventType or "node"
    content:    dict[str, Any]
    score:      float            # relevance score 0.0–1.0
    timestamp:  str = ""
    agent:      str = ""


@dataclass
class QueryResponse:
    """Full response from a context query."""
    query:    str
    results:  list[ContextResult]
    total:    int
    elapsed_ms: float
    sources:  list[str]

    def to_dict(self) -> dict:
        return {
            "query":      self.query,
            "total":      self.total,
            "elapsed_ms": self.elapsed_ms,
            "sources":    self.sources,
            "results": [
                {
                    "source":     r.source,
                    "event_type": r.event_type,
                    "content":    r.content,
                    "score":      r.score,
                    "timestamp":  r.timestamp,
                    "agent":      r.agent,
                }
                for r in self.results
            ],
        }


@dataclass
class AgentRegistration:
    """An external agent registered to receive context dispatches."""
    agent_id:    str
    description: str
    tags:        list[str]
    query_fn:    Callable[[str], Coroutine] | None = field(default=None, repr=False)
    registered_at: float = field(default_factory=time.monotonic)


# ── MemoryBus ─────────────────────────────────────────────────────────────────

class MemoryBus:
    """
    Lightweight in-process pub/sub bus for event propagation.

    External agents subscribe to event types; the HubConnector publishes
    every ledger event through the bus so agents receive real-time updates
    without polling.

    Example::

        bus = MemoryBus()
        bus.subscribe(my_callback, filter_types=[EventType.NODE_APPROVED])
        bus.publish(EventType.NODE_APPROVED, {"summary": "JWT implemented"})
    """

    def __init__(self) -> None:
        self._subscribers: dict[str | None, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(
        self,
        callback: Callable[[str, dict], None],
        filter_types: list[EventType | str] | None = None,
    ) -> None:
        """
        Register a callback to receive events.

        callback    : func(event_type: str, content: dict) → None
        filter_types: if None, receives ALL event types
        """
        with self._lock:
            if filter_types is None:
                self._subscribers[None].append(callback)
            else:
                for et in filter_types:
                    key = et.value if isinstance(et, EventType) else et
                    self._subscribers[key].append(callback)

    def publish(self, event_type: EventType | str, content: dict) -> None:
        """Broadcast an event to all matching subscribers."""
        key = event_type.value if isinstance(event_type, EventType) else event_type
        with self._lock:
            callbacks = self._subscribers[None] + self._subscribers.get(key, [])

        for cb in callbacks:
            try:
                cb(key, content)
            except Exception as exc:
                logger.debug(f"[MemoryBus] subscriber error: {exc}")

    def unsubscribe_all(self) -> None:
        with self._lock:
            self._subscribers.clear()


# ── HubConnector ──────────────────────────────────────────────────────────────

class HubConnector:
    """
    Universal Hub bridge — exposes Nexus memory to external agents.

    Parameters
    ──────────
    db_path       : SQLite database path (shared with main system).
    project_root  : Root directory for the local file indexer.
    host          : HTTP server host (default 0.0.0.0).
    port          : HTTP server port (default 9000).
    top_k_default : Default number of results per query.
    """

    def __init__(
        self,
        db_path:      str  = "data/contextforge.db",
        project_root: str  = ".",
        host:         str  = "0.0.0.0",
        port:         int  = 9000,
        top_k_default: int = 5,
        charter_path: str  = "PROJECT_CHARTER.md",
    ) -> None:
        self._db_path      = db_path
        self._host         = host
        self._port         = port
        self._top_k        = top_k_default
        self._charter_path = charter_path

        # Core subsystems
        self._ledger  = EventLedger(db_path=db_path, charter_path=charter_path)
        self._storage = StorageAdapter(db_path=db_path)
        self._indexer = LocalIndexer(project_root=project_root, threshold=0.65)
        self._bus     = MemoryBus()

        # Registered external agents
        self._agents:      dict[str, AgentRegistration]  = {}
        # Per-agent permission policies (keyed by agent_id)
        self._permissions: dict[str, PermissionPolicy]   = {}

        # HTTP server thread
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Warm the index on init (non-blocking)
        threading.Thread(
            target=self._indexer.build_index,
            daemon=True,
            name="hub-index-warm",
        ).start()

        logger.info(
            f"[HubConnector] init  db={db_path}  port={port}"
        )

    # -----------------------------------------------------------------------
    # Core query methods (usable without HTTP)
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Permission management
    # -----------------------------------------------------------------------

    def set_agent_permissions(
        self,
        agent_id:            str,
        allowed_event_types: list[str] | None = None,
        blocked_event_types: list[str] | None = None,
    ) -> None:
        """
        Set or replace the ``PermissionPolicy`` for a registered agent.

        Example::

            hub.set_agent_permissions(
                "job_agent",
                allowed_event_types=["user_input", "research", "node_approved", "task_done"],
                blocked_event_types=["agent_thought", "file_diff"],
            )
        """
        policy = PermissionPolicy(
            allowed_event_types = allowed_event_types or [],
            blocked_event_types = blocked_event_types or [],
        )
        self._permissions[agent_id] = policy
        logger.info(
            f"[HubConnector] Permissions set for {agent_id}  "
            f"allow={policy.allowed_event_types}  block={policy.blocked_event_types}"
        )

    def _get_policy(self, agent_id: str | None) -> PermissionPolicy | None:
        """Return the policy for *agent_id*, or None if no policy is registered."""
        if agent_id is None:
            return None
        return self._permissions.get(agent_id)

    def _filter_results(
        self,
        results:  list[ContextResult],
        agent_id: str | None,
    ) -> list[ContextResult]:
        """Remove results that the *agent_id* is not permitted to see.

        All event type comparisons are normalised to lowercase so that ledger
        values ("CONFLICT") and test fixtures ("conflict") both work correctly.
        """
        policy = self._get_policy(agent_id)
        if policy is None:
            # No per-agent policy — still apply system-level protection
            return [r for r in results if r.event_type.lower() not in _SYSTEM_PROTECTED_TYPES]
        return [r for r in results if policy.is_permitted(r.event_type)]

    # -----------------------------------------------------------------------
    # Core query methods (usable without HTTP)
    # -----------------------------------------------------------------------

    async def query(
        self,
        q:        str,
        top_k:    int            = 5,
        sources:  list[str] | None = None,
        agent_id: str | None     = None,
    ) -> QueryResponse:
        """
        Unified semantic query across all memory sources.

        sources  : ["ledger", "graph", "local_index"] or None (all)
        agent_id : if provided, applies the registered PermissionPolicy for
                   that agent before returning results.
        """
        t0      = time.monotonic()
        sources = sources or ["ledger", "graph", "local_index"]
        results: list[ContextResult] = []

        # ── Source 1: Event ledger (recent history) ──────────────────────
        if "ledger" in sources:
            events = await asyncio.to_thread(
                self._ledger.list_events, top_k * 3, None, "active"
            )
            q_lower = q.lower()
            for ev in events:
                content = ev.get("content", {})
                text    = json.dumps(content).lower()
                score   = self._keyword_score(q_lower, text)
                if score > 0.1:
                    results.append(ContextResult(
                        source     = "ledger",
                        event_type = ev.get("event_type", ""),
                        content    = content,
                        score      = round(score, 4),
                        timestamp  = ev.get("created_at", ""),
                        agent      = content.get("agent", ""),
                    ))

        # ── Source 2: Knowledge graph nodes ─────────────────────────────
        if "graph" in sources:
            try:
                nodes = self._storage.search_nodes(
                    project_id = os.getenv("PROJECT_ID", "contextforge-default"),
                    query      = q,
                    limit      = top_k,
                    status     = "active",
                )
                for node in nodes:
                    results.append(ContextResult(
                        source     = "graph",
                        event_type = "node",
                        content    = {
                            "summary":   node.get("summary", ""),
                            "rationale": node.get("rationale", ""),
                            "area":      node.get("area", ""),
                        },
                        score      = float(node.get("confidence", 0.5)),
                        timestamp  = str(node.get("created_at", "")),
                        agent      = node.get("agent", ""),
                    ))
            except Exception as exc:
                logger.debug(f"[HubConnector] graph search error: {exc}")

        # ── Source 3: Local file index ───────────────────────────────────
        if "local_index" in sources:
            try:
                hits = await asyncio.to_thread(
                    self._indexer.search, q, top_k, 0.65
                )
                for hit in hits:
                    results.append(ContextResult(
                        source     = "local_index",
                        event_type = "file_chunk",
                        content    = {"file": hit["file"], "text": hit["text"]},
                        score      = hit["score"],
                        timestamp  = "",
                        agent      = "indexer",
                    ))
            except Exception as exc:
                logger.debug(f"[HubConnector] indexer search error: {exc}")

        # Apply permission filter BEFORE ranking so denied results don't
        # count against the top_k budget.
        results = self._filter_results(results, agent_id)

        # Rank by score, deduplicate by content hash
        seen:    set[str] = set()
        ranked:  list[ContextResult] = []
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            h = hashlib.sha256(json.dumps(r.content, sort_keys=True).encode()).hexdigest()[:16]
            if h not in seen:
                seen.add(h)
                ranked.append(r)
            if len(ranked) >= top_k:
                break

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return QueryResponse(
            query      = q,
            results    = ranked,
            total      = len(ranked),
            elapsed_ms = round(elapsed_ms, 2),
            sources    = list({r.source for r in ranked}),
        )

    async def get_entity(self, entity_name: str, top_k: int = 5) -> QueryResponse:
        """
        Focused entity lookup — e.g., "JWT service", "Project goals", "User hobbies".
        Searches all sources with entity_name as the query, then filters for
        results where the entity name appears verbatim in the content.
        """
        response = await self.query(entity_name, top_k=top_k * 2)
        name_lc  = entity_name.lower()
        filtered = [
            r for r in response.results
            if name_lc in json.dumps(r.content).lower()
        ]
        response.results    = filtered[:top_k]
        response.total      = len(response.results)
        response.sources    = list({r.source for r in response.results})
        return response

    async def get_history(
        self,
        n:          int          = 20,
        agent:      str | None   = None,
        event_type: str | None   = None,
        agent_id:   str | None   = None,
    ) -> list[dict]:
        """
        Return recent event history, optionally filtered by agent or event type.

        agent_id : caller identity for permission enforcement.  System-protected
                   event types (CONFLICT, ROLLBACK, CHECKPOINT) are always removed.
        """
        events = await asyncio.to_thread(
            self._ledger.list_events, n * 3, event_type, "active"
        )
        if agent:
            events = [e for e in events if e.get("content", {}).get("agent") == agent]

        # Permission filtering
        policy = self._get_policy(agent_id)
        if policy is not None:
            events = [e for e in events if policy.is_permitted(e.get("event_type", ""))]
        else:
            events = [
                e for e in events
                if e.get("event_type", "").lower() not in _SYSTEM_PROTECTED_TYPES
            ]

        return events[:n]

    async def export_memory(self, project_id: str | None = None) -> dict:
        """
        Export a structured JSON snapshot of the entire active memory:
          - Recent events (last 100)
          - Active knowledge graph nodes
          - Ledger reconstruct_state() system prompt
        """
        pid     = project_id or os.getenv("PROJECT_ID", "contextforge-default")
        events  = await asyncio.to_thread(self._ledger.list_events, 100, None, "active")
        try:
            nodes = self._storage.search_nodes(pid, "", limit=50, status="active")
        except Exception:
            nodes = []
        state   = await asyncio.to_thread(self._ledger.reconstruct_state, 20)

        return {
            "project_id":       pid,
            "exported_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_count":      len(events),
            "node_count":       len(nodes),
            "events":           events[:100],
            "nodes":            nodes[:50],
            "system_prompt":    state,
        }

    # -----------------------------------------------------------------------
    # External agent registry
    # -----------------------------------------------------------------------

    def register_agent(
        self,
        agent_id:    str,
        description: str,
        tags:        list[str] | None = None,
        query_fn:    Callable | None  = None,
    ) -> None:
        """
        Register an external agent with the hub.

        agent_id    : Unique identifier (e.g., "job_agent", "scene_sorter").
        description : Human-readable description of what the agent does.
        tags        : Keywords for dispatch routing.
        query_fn    : Optional async function(query: str) → str for bidirectional dispatch.
        """
        self._agents[agent_id] = AgentRegistration(
            agent_id    = agent_id,
            description = description,
            tags        = tags or [],
            query_fn    = query_fn,
        )
        logger.info(f"[HubConnector] Agent registered: {agent_id}  tags={tags}")

    def unregister_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    async def dispatch(self, query: str, agent_id: str | None = None) -> dict:
        """
        Dispatch a query to:
          - A specific registered agent (if agent_id given), or
          - The best-matching agent based on tag overlap with the query.

        Returns the agent's response + the Nexus context it would receive.
        """
        context  = await self.query(query, top_k=self._top_k)
        context_str = json.dumps(context.to_dict(), indent=2)

        target: AgentRegistration | None = None
        if agent_id:
            target = self._agents.get(agent_id)
        else:
            # Tag-based routing — find agent with most tag matches
            q_lower  = query.lower()
            best_score = -1
            for reg in self._agents.values():
                score = sum(1 for tag in reg.tags if tag.lower() in q_lower)
                if score > best_score:
                    best_score = score
                    target     = reg

        agent_response: str = ""
        if target and target.query_fn:
            try:
                agent_response = await target.query_fn(
                    f"CONTEXT:\n{context_str}\n\nQUERY: {query}"
                )
            except Exception as exc:
                agent_response = f"[dispatch error] {exc}"

        return {
            "query":          query,
            "routed_to":      target.agent_id if target else None,
            "context":        context.to_dict(),
            "agent_response": agent_response,
        }

    # -----------------------------------------------------------------------
    # MemoryBus access
    # -----------------------------------------------------------------------

    @property
    def bus(self) -> MemoryBus:
        """Access the MemoryBus for subscribing to real-time ledger events."""
        return self._bus

    def publish(self, event_type: EventType | str, content: dict) -> None:
        """Publish an event to the MemoryBus (call after ledger.append)."""
        self._bus.publish(event_type, content)

    # -----------------------------------------------------------------------
    # HTTP server (FastAPI)
    # -----------------------------------------------------------------------

    def start(self, daemon: bool = True) -> None:
        """Start the Context-API HTTP server in a background thread."""
        if not _FASTAPI_AVAILABLE:
            logger.warning(
                "[HubConnector] FastAPI/uvicorn not installed — HTTP server disabled. "
                "Use in-process API only. Install with: pip install fastapi uvicorn"
            )
            return
        if self._server_thread and self._server_thread.is_alive():
            return

        self._server_thread = threading.Thread(
            target  = self._run_server,
            daemon  = daemon,
            name    = "hub-http-server",
        )
        self._server_thread.start()
        logger.info(f"[HubConnector] HTTP server starting on http://{self._host}:{self._port}")

    def shutdown(self) -> None:
        self._stop_event.set()
        logger.info("[HubConnector] Shutdown requested")

    def _run_server(self) -> None:
        if not _FASTAPI_AVAILABLE:
            return

        app = FastAPI(
            title       = "ContextForge Nexus — Context API",
            description = "External agent bridge for the Nexus Event-Sourced Memory",
            version     = "5.0.0",
            docs_url    = "/",
        )
        hub = self   # capture for closures

        # ── Health ──────────────────────────────────────────────────────
        @app.get("/context/health")
        async def health():
            return {
                "status":           "ok",
                "registered_agents": list(hub._agents.keys()),
                "indexer_chunks":   hub._indexer.stats().get("chunks", 0),
                "version":          "5.0.0",
            }

        # ── Query ────────────────────────────────────────────────────────
        @app.get("/context/query")
        async def context_query(
            q:        str          = Query(..., description="Search query"),
            top_k:    int          = Query(5, ge=1, le=50, description="Max results"),
            sources:  str          = Query("ledger,graph,local_index", description="Comma-separated sources"),
            agent_id: str | None   = Query(None, description="Caller agent ID for permission gating"),
        ):
            src_list = [s.strip() for s in sources.split(",")]
            result   = await hub.query(q, top_k=top_k, sources=src_list, agent_id=agent_id)
            return JSONResponse(result.to_dict())

        # ── Entity lookup ────────────────────────────────────────────────
        @app.get("/context/entity/{entity_name}")
        async def context_entity(
            entity_name: str,
            top_k:    int        = Query(5, ge=1, le=20),
            agent_id: str | None = Query(None, description="Caller agent ID for permission gating"),
        ):
            result = await hub.get_entity(entity_name, top_k=top_k)
            if agent_id:
                result.results = hub._filter_results(result.results, agent_id)
                result.total   = len(result.results)
            return JSONResponse(result.to_dict())

        # ── History ─────────────────────────────────────────────────────
        @app.get("/context/history")
        async def context_history(
            n:          int        = Query(20, ge=1, le=500),
            agent:      str | None = Query(None),
            event_type: str | None = Query(None),
            agent_id:   str | None = Query(None, description="Caller agent ID for permission gating"),
        ):
            events = await hub.get_history(n=n, agent=agent, event_type=event_type, agent_id=agent_id)
            return JSONResponse({"count": len(events), "events": events})

        # ── Export ──────────────────────────────────────────────────────
        @app.post("/context/export")
        async def context_export(project_id: str | None = None):
            snapshot = await hub.export_memory(project_id=project_id)
            return JSONResponse(snapshot)

        # ── Dispatch ────────────────────────────────────────────────────
        @app.post("/context/dispatch")
        async def context_dispatch(body: dict):
            q        = body.get("query", "")
            agent_id = body.get("agent_id")
            if not q:
                raise HTTPException(status_code=400, detail="query is required")
            result = await hub.dispatch(q, agent_id=agent_id)
            return JSONResponse(result)

        # ── Agents ──────────────────────────────────────────────────────
        @app.get("/context/agents")
        async def context_agents():
            return JSONResponse({
                aid: {"description": reg.description, "tags": reg.tags}
                for aid, reg in hub._agents.items()
            })

        uvicorn.run(app, host=self._host, port=self._port, log_level="error")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _keyword_score(query: str, text: str) -> float:
        """Simple term-overlap relevance score (no external deps)."""
        q_terms = set(query.lower().split())
        t_terms = set(text.lower().split())
        if not q_terms or not t_terms:
            return 0.0
        overlap = len(q_terms & t_terms)
        return overlap / math.sqrt(len(q_terms) * len(t_terms))


import math  # imported here to avoid circular import position issues


# ── Module-level singleton ────────────────────────────────────────────────────

_hub_singleton: HubConnector | None = None


def get_hub(
    db_path:      str = "data/contextforge.db",
    project_root: str = ".",
    port:         int = 9000,
) -> HubConnector:
    """Return (or create) the process-level HubConnector singleton."""
    global _hub_singleton
    if _hub_singleton is None:
        _hub_singleton = HubConnector(
            db_path      = db_path,
            project_root = project_root,
            port         = port,
        )
    return _hub_singleton
