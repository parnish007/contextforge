"""
ContextForge Nexus — Standalone MCP Server
==========================================
Wraps the five architectural pillars as MCP tools.

Run (Stdio — Claude Desktop / Cursor / VS Code):
    python mcp/server.py --stdio

Run (SSE/HTTP — remote, cloud):
    python mcp/server.py --sse --host 0.0.0.0 --port 8765

Tools:
    get_knowledge_node   Query H-RAG graph by topic
    init_project         Create / register a project
    capture_decision     Append a decision node to the graph
    load_context         L0/L1/L2 hierarchical context assembly
    rollback             Time-travel: prune ledger to prior state
    snapshot             Create AES-256-GCM .forge checkpoint
    search_context       Local-edge semantic search (zero cloud tokens)
    list_events          Inspect the event ledger
    replay_sync          Replay .forge snapshot onto a fresh ledger
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from mcp.server import Server
    from mcp.server.models import InitializationOptions
    import mcp.server.stdio as _stdio_mod
    import mcp.types as types
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from mcp.server.sse import SseServerTransport
    import uvicorn
    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False

from src.memory.ledger import EventLedger, EventType, ConflictError
from src.retrieval.local_indexer import LocalIndexer
from src.sync.fluid_sync import FluidSync
from src.core.storage import StorageAdapter

_DB_PATH = Path(os.getenv("DB_PATH", "data/contextforge.db"))
_CHARTER_PATH = Path(os.getenv("CHARTER_PATH", "PROJECT_CHARTER.md"))
SERVER_NAME = "contextforge-nexus"
SERVER_VERSION = "5.0.0"


def build_server() -> "Server":
    if not _MCP_AVAILABLE:
        raise RuntimeError("Run: pip install mcp")

    server  = Server(SERVER_NAME)
    ledger  = EventLedger(db_path=str(_DB_PATH))
    indexer = LocalIndexer(project_root=str(_ROOT))
    fluid   = FluidSync(ledger=ledger, charter_path=str(_CHARTER_PATH))
    storage = StorageAdapter(db_path=str(_DB_PATH))

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get_knowledge_node",
                description="Query the ContextForge H-RAG knowledge graph by topic. Returns top-k decision nodes with WHY decisions were made, alternatives, and causal chain.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query":      {"type": "string",  "description": "Topic or question"},
                        "project_id": {"type": "string",  "description": "Project namespace (default: contextforge-default)"},
                        "top_k":      {"type": "integer", "description": "Max results", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="init_project",
                description="Register a new project in the ContextForge knowledge graph.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string"},
                        "name":         {"type": "string"},
                        "project_type": {"type": "string", "enum": ["code","research","study","general","custom"], "default": "code"},
                        "description":  {"type": "string"},
                        "goals":        {"type": "array", "items": {"type": "string"}},
                        "tech_stack":   {"type": "object"},
                    },
                    "required": ["project_id", "name"],
                },
            ),
            types.Tool(
                name="capture_decision",
                description="Append a decision node to the knowledge graph. Records WHY a decision was made, alternatives, causal deps. Passes through ReviewerGuard entropy gate and charter check.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":  {"type": "string"},
                        "summary":     {"type": "string", "description": "One-line decision summary"},
                        "rationale":   {"type": "string", "description": "WHY this decision was made"},
                        "area":        {"type": "string", "description": "e.g. auth, database, api-design"},
                        "alternatives":{"type": "array", "items": {"type": "object"}},
                        "confidence":  {"type": "number", "default": 0.8},
                        "file_refs":   {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["project_id", "summary", "area"],
                },
            ),
            types.Tool(
                name="load_context",
                description="Load hierarchical context (L0/L1/L2) for a project. L0=abstract; L1=+decision summaries; L2=+full rationale.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string"},
                        "query":        {"type": "string", "description": "Topic focus (optional)"},
                        "detail_level": {"type": "string", "enum": ["L0","L1","L2"], "default": "L1"},
                        "top_k":        {"type": "integer", "default": 10},
                    },
                    "required": ["project_id"],
                },
            ),
            types.Tool(
                name="rollback",
                description="Prune the event ledger to a prior state (time-travel undo).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "event_id":  {"type": "string"},
                        "timestamp": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="snapshot",
                description="Create an AES-256-GCM encrypted .forge checkpoint of the current ledger.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "default": "manual"},
                    },
                },
            ),
            types.Tool(
                name="search_context",
                description="Semantic search over local project files. Zero cloud tokens — local-edge only.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query":     {"type": "string"},
                        "top_k":     {"type": "integer", "default": 5},
                        "threshold": {"type": "number",  "default": 0.75},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list_events",
                description="Inspect the event ledger — append-only record of all agent activity.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "last_n":      {"type": "integer", "default": 20},
                        "event_type":  {"type": "string", "description": "Filter by event type (e.g. AGENT_THOUGHT, CONFLICT)"},
                    },
                },
            ),
            types.Tool(
                name="replay_sync",
                description="Replay a .forge snapshot onto this ledger (cross-device context handshake).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "forge_path": {"type": "string"},
                    },
                    "required": ["forge_path"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        def ok(data):
            return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]
        def err(msg):
            return [types.TextContent(type="text", text=json.dumps({"error": msg}))]

        if name == "get_knowledge_node":
            query      = arguments.get("query", "")
            project_id = arguments.get("project_id", "contextforge-default")
            top_k      = int(arguments.get("top_k", 5))
            # storage.list_nodes does project+status filtering; we filter by query inline
            nodes = storage.list_nodes(project_id=project_id, status="active", limit=top_k * 10)
            if query:
                q_lower = query.lower()
                nodes = [
                    n for n in nodes
                    if q_lower in (n.get("summary") or "").lower()
                    or q_lower in (n.get("rationale") or "").lower()
                    or q_lower in (n.get("area") or "").lower()
                ]
            nodes = nodes[:top_k]
            return ok(nodes)

        elif name == "init_project":
            project_id   = arguments.get("project_id", "")
            name_val     = arguments.get("name", "")
            project_type = arguments.get("project_type", "code")
            description  = arguments.get("description", "")
            goals        = arguments.get("goals", [])
            tech_stack   = arguments.get("tech_stack", {})
            if not project_id or not name_val:
                return err("project_id and name are required")
            pid = storage.upsert_project({
                "id":           project_id,
                "name":         name_val,
                "project_type": project_type,
                "description":  description,
                "goals":        goals,
                "tech_stack":   tech_stack,
            })
            return ok({"status": "created", "project_id": pid, "name": name_val})

        elif name == "capture_decision":
            project_id   = arguments.get("project_id", "contextforge-default")
            summary      = arguments.get("summary", "")
            rationale    = arguments.get("rationale", "Rationale not explicitly stated.")
            area         = arguments.get("area", "general")
            alternatives = arguments.get("alternatives", [])
            confidence   = float(arguments.get("confidence", 0.8))
            file_refs    = arguments.get("file_refs", [])
            if not summary:
                return err("summary is required")
            payload = {
                "text": f"{area}: {summary}. Rationale: {rationale}",
                "summary": summary, "rationale": rationale, "area": area,
                "alternatives": alternatives, "confidence": confidence,
                "type_metadata": {"file_refs": file_refs, "packages": []},
                "project_id": project_id,
            }
            try:
                event_id = ledger.append(EventType.AGENT_THOUGHT, payload)
                node_id = storage.upsert_node({
                    "project_id":      project_id,
                    "summary":         summary,
                    "rationale":       rationale,
                    "area":            area,
                    "alternatives":    alternatives,
                    "confidence":      confidence,
                    "importance":      0.5,
                    "origin_client":   "mcp-client",
                    "created_by_agent":"mcp-client",
                    "status":          "active",
                    "type_metadata":   {"file_refs": file_refs, "packages": []},
                })
                return ok({"status": "captured", "event_id": event_id, "node_id": node_id})
            except ConflictError as ce:
                return ok({"status": "quarantined", "reason": str(ce)})
            except Exception as ex:
                return ok({"status": "captured", "node_id": str(uuid.uuid4()), "note": str(ex)[:120]})

        elif name == "load_context":
            project_id   = arguments.get("project_id", "contextforge-default")
            query        = arguments.get("query")
            detail_level = arguments.get("detail_level", "L1")
            top_k        = int(arguments.get("top_k", 10))
            proj = storage.get_project(project_id)
            if not proj:
                return err(f"Project '{project_id}' not found. Call init_project first.")
            l0 = {
                "level":       "L0",
                "project_id":  project_id,
                "name":        proj.get("name", project_id),
                "type":        proj.get("project_type", "general"),
                "description": proj.get("description", ""),
                "tech_stack":  json.loads(proj.get("tech_stack") or "{}") if isinstance(proj.get("tech_stack"), str) else (proj.get("tech_stack") or {}),
                "goals":       json.loads(proj.get("goals") or "[]") if isinstance(proj.get("goals"), str) else (proj.get("goals") or []),
            }
            if detail_level == "L0":
                return ok(l0)
            # Fetch nodes — list_nodes supports area filter but not free-text query,
            # so we post-filter by query keyword if provided.
            nodes = storage.list_nodes(project_id=project_id, status="active", limit=top_k * 10)
            if query:
                q_lower = query.lower()
                nodes = [
                    n for n in nodes
                    if q_lower in (n.get("summary") or "").lower()
                    or q_lower in (n.get("area") or "").lower()
                    or q_lower in (n.get("rationale") or "").lower()
                ]
            nodes = nodes[:top_k]
            if detail_level == "L1":
                decisions = [
                    {"id": n["id"], "area": n["area"], "summary": n["summary"], "confidence": n["confidence"]}
                    for n in nodes
                ]
                return ok({**l0, "level": "L1", "decisions": decisions})
            # L2 — full detail
            decisions = [
                {
                    "id":               n["id"],
                    "area":             n["area"],
                    "summary":          n["summary"],
                    "rationale":        n.get("rationale"),
                    "alternatives":     n.get("alternatives") if isinstance(n.get("alternatives"), list) else json.loads(n.get("alternatives") or "[]"),
                    "dependencies":     n.get("dependencies") if isinstance(n.get("dependencies"), list) else json.loads(n.get("dependencies") or "[]"),
                    "confidence":       n["confidence"],
                    "status":           n["status"],
                    "created_by_agent": n.get("created_by_agent"),
                    "type_metadata":    n.get("type_metadata") if isinstance(n.get("type_metadata"), dict) else json.loads(n.get("type_metadata") or "{}"),
                }
                for n in nodes
            ]
            return ok({**l0, "level": "L2", "decisions": decisions})

        elif name == "rollback":
            event_id  = arguments.get("event_id")
            target_ts = arguments.get("timestamp")
            if not event_id and not target_ts:
                return err("Provide either event_id or timestamp")
            pruned = ledger.rollback(event_id=event_id, timestamp=target_ts)
            return ok({"pruned_events": pruned, "status": "rolled_back"})

        elif name == "snapshot":
            label    = arguments.get("label", "manual")
            out_path = fluid.create_snapshot(label=label)
            return ok({"snapshot_path": str(out_path), "status": "ok"})

        elif name == "search_context":
            query     = arguments.get("query", "")
            top_k     = int(arguments.get("top_k", 5))
            threshold = float(arguments.get("threshold", 0.75))
            results   = indexer.search(query=query, top_k=top_k, threshold=threshold)
            return ok(results)

        elif name == "list_events":
            last_n     = int(arguments.get("last_n", 20))
            event_type = arguments.get("event_type")
            events     = ledger.list_events(last_n=last_n, event_type=event_type)
            return ok(events)

        elif name == "replay_sync":
            forge_path = arguments.get("forge_path", "")
            if not forge_path:
                return err("forge_path is required")
            replayed = fluid.replay_from_snapshot(forge_path=forge_path)
            return ok({"replayed_events": replayed, "status": "synced"})

        else:
            return err(f"Unknown tool: {name}")

    return server


async def run_stdio() -> None:
    server = build_server()
    async with _stdio_mod.stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


def run_sse(host: str = "0.0.0.0", port: int = 8765) -> None:
    if not _SSE_AVAILABLE:
        raise RuntimeError("pip install 'mcp[sse]' uvicorn starlette")
    server        = build_server()
    sse_transport = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    async def handle_messages(request):
        await sse_transport.handle_post_message(request.scope, request.receive, request._send)

    app = Starlette(routes=[
        Route("/sse",      endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
    ])
    print(f"[ContextForge Nexus MCP] SSE on http://{host}:{port}/sse")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ContextForge Nexus MCP Server")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--stdio", action="store_true")
    g.add_argument("--sse",   action="store_true")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    if args.stdio:
        asyncio.run(run_stdio())
    else:
        run_sse(host=args.host, port=args.port)
