"""
ContextForge Nexus Architecture — Dual-Transport MCP Server
=====================================================

Entry point for the Nexus MCP server.

Transport Modes:
  Stdio  — primary, for Claude Desktop / Cursor / local IDE integration.
           Launch: python -m src.transport.server --stdio
  SSE    — secondary, for remote / cloud access via FastAPI endpoint.
           Launch: python -m src.transport.server --sse [--host 0.0.0.0 --port 8765]

Exposed MCP Tools:
  get_knowledge_node   — query the H-RAG context by topic
  rollback             — prune ledger to a prior event_id or ISO timestamp
  snapshot             — create an encrypted .forge checkpoint bundle
  search_context       — local-edge semantic search over project files
  list_events          — inspect the event ledger (last N entries)
  replay_sync          — replay an event log onto a fresh ledger (new-device handshake)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Conditional imports — SSE transport requires uvicorn + starlette
# ---------------------------------------------------------------------------
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
    from starlette.routing import Route, Mount
    from mcp.server.sse import SseServerTransport
    import uvicorn
    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False

# Internal modules
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.memory.ledger import EventLedger, EventType
from src.retrieval.local_indexer import LocalIndexer
from src.sync.fluid_sync import FluidSync
from src.core.storage import StorageAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_NAME = "contextforge-nexus"
SERVER_VERSION = "5.0.0"
_DB_PATH = Path(os.getenv("DB_PATH", "data/contextforge.db"))
_CHARTER_PATH = Path(os.getenv("CHARTER_PATH", "PROJECT_CHARTER.md"))


# ---------------------------------------------------------------------------
# Server factory — builds the MCP Server with all tools registered
# ---------------------------------------------------------------------------

def build_server() -> "Server":
    """Construct and wire the MCP Server instance."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: pip install mcp"
        )

    server = Server(SERVER_NAME)
    ledger = EventLedger(db_path=str(_DB_PATH))
    indexer = LocalIndexer(project_root=str(Path.cwd()))
    fluid = FluidSync(ledger=ledger, charter_path=str(_CHARTER_PATH))
    storage = StorageAdapter(db_path=str(_DB_PATH))

    # -----------------------------------------------------------------------
    # Tool: get_knowledge_node
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def get_knowledge_node(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "get_knowledge_node":
            raise ValueError(f"Unknown tool: {name}")

        query: str = arguments.get("query", "")
        project_id: str = arguments.get("project_id", "contextforge-default")
        top_k: int = int(arguments.get("top_k", 5))

        nodes = storage.search_nodes(
            project_id=project_id,
            query=query,
            limit=top_k,
            status="active",
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(nodes, indent=2, default=str),
        )]

    # -----------------------------------------------------------------------
    # Tool: rollback
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def rollback(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "rollback":
            raise ValueError(f"Unknown tool: {name}")

        event_id: str | None = arguments.get("event_id")
        target_ts: str | None = arguments.get("timestamp")

        if not event_id and not target_ts:
            return [types.TextContent(
                type="text",
                text='{"error": "Provide either event_id or timestamp"}',
            )]

        pruned = ledger.rollback(event_id=event_id, timestamp=target_ts)
        return [types.TextContent(
            type="text",
            text=json.dumps({"pruned_events": pruned, "status": "rolled_back"}, indent=2),
        )]

    # -----------------------------------------------------------------------
    # Tool: snapshot
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def snapshot(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "snapshot":
            raise ValueError(f"Unknown tool: {name}")

        label: str = arguments.get("label", "manual")
        out_path = fluid.create_snapshot(label=label)
        return [types.TextContent(
            type="text",
            text=json.dumps({"snapshot_path": str(out_path), "status": "ok"}, indent=2),
        )]

    # -----------------------------------------------------------------------
    # Tool: search_context
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def search_context(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "search_context":
            raise ValueError(f"Unknown tool: {name}")

        query: str = arguments.get("query", "")
        top_k: int = int(arguments.get("top_k", 5))
        threshold: float = float(arguments.get("threshold", 0.75))

        results = indexer.search(query=query, top_k=top_k, threshold=threshold)
        return [types.TextContent(
            type="text",
            text=json.dumps(results, indent=2, default=str),
        )]

    # -----------------------------------------------------------------------
    # Tool: list_events
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def list_events(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "list_events":
            raise ValueError(f"Unknown tool: {name}")

        last_n: int = int(arguments.get("last_n", 20))
        event_type: str | None = arguments.get("type")
        events = ledger.list_events(last_n=last_n, event_type=event_type)
        return [types.TextContent(
            type="text",
            text=json.dumps(events, indent=2, default=str),
        )]

    # -----------------------------------------------------------------------
    # Tool: replay_sync
    # -----------------------------------------------------------------------
    @server.call_tool()
    async def replay_sync(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        if name != "replay_sync":
            raise ValueError(f"Unknown tool: {name}")

        forge_path: str = arguments.get("forge_path", "")
        if not forge_path:
            return [types.TextContent(
                type="text",
                text='{"error": "forge_path is required"}',
            )]

        replayed = fluid.replay_from_snapshot(forge_path=forge_path)
        return [types.TextContent(
            type="text",
            text=json.dumps({"replayed_events": replayed, "status": "synced"}, indent=2),
        )]

    # -----------------------------------------------------------------------
    # Register tool schemas (list_tools handler)
    # -----------------------------------------------------------------------
    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get_knowledge_node",
                description="Query the ContextForge H-RAG knowledge graph by topic.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query":      {"type": "string", "description": "Search query"},
                        "project_id": {"type": "string", "description": "Project namespace"},
                        "top_k":      {"type": "integer", "description": "Max results", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="rollback",
                description="Prune the event ledger to a prior state (undo).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "event_id":  {"type": "string", "description": "Target event UUID"},
                        "timestamp": {"type": "string", "description": "ISO 8601 target timestamp"},
                    },
                },
            ),
            types.Tool(
                name="snapshot",
                description="Create an encrypted .forge checkpoint of the current ledger.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Human label for the snapshot"},
                    },
                },
            ),
            types.Tool(
                name="search_context",
                description="Semantic search over local project files (local-edge, zero cloud tokens).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query":     {"type": "string", "description": "Search query"},
                        "top_k":     {"type": "integer", "default": 5},
                        "threshold": {"type": "number",  "default": 0.75,
                                      "description": "Min cosine similarity"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list_events",
                description="Inspect the event ledger.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "last_n": {"type": "integer", "default": 20},
                        "type":   {"type": "string", "description": "Filter by event type"},
                    },
                },
            ),
            types.Tool(
                name="replay_sync",
                description="Replay a .forge snapshot onto this ledger (new-device handshake).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "forge_path": {"type": "string", "description": "Path to .forge file"},
                    },
                    "required": ["forge_path"],
                },
            ),
        ]

    return server


# ---------------------------------------------------------------------------
# Transport runners
# ---------------------------------------------------------------------------

async def run_stdio() -> None:
    """Run server over stdio (Claude Desktop / Cursor)."""
    server = build_server()
    async with _stdio_mod.stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def run_sse(host: str = "0.0.0.0", port: int = 8765) -> None:
    """Run server over HTTP/SSE via Starlette + uvicorn."""
    if not _SSE_AVAILABLE:
        raise RuntimeError(
            "SSE transport requires: pip install mcp[sse] uvicorn starlette"
        )

    server = build_server()
    sse_transport = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    async def handle_messages(request):
        await sse_transport.handle_post_message(request.scope, request.receive, request._send)

    app = Starlette(
        routes=[
            Route("/sse",      endpoint=handle_sse),
            Route("/messages", endpoint=handle_messages, methods=["POST"]),
        ]
    )

    print(f"[Nexus] SSE transport listening on http://{host}:{port}/sse")
    uvicorn.run(app, host=host, port=port)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ContextForge Nexus Architecture MCP Server")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true",  help="Run over stdio")
    group.add_argument("--sse",   action="store_true",  help="Run over HTTP/SSE")
    parser.add_argument("--host", default="0.0.0.0",   help="SSE host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="SSE port (default 8765)")
    args = parser.parse_args()

    if args.stdio:
        asyncio.run(run_stdio())
    else:
        run_sse(host=args.host, port=args.port)
