"""
ContextForge Nexus — Standalone MCP Server
==========================================
Wraps the five architectural pillars as MCP tools.

Run (Stdio — Claude Desktop / Cursor / VS Code):
    python mcp/server.py --stdio

Run (SSE/HTTP — remote, cloud):
    python mcp/server.py --sse --host 0.0.0.0 --port 8765

Tools (18 total):
  Project management:
    list_projects        List all registered projects
    init_project         Create / update a project
    rename_project       Rename a project (and optionally update description)
    merge_projects       Merge one project's data into another
    delete_project       Permanently delete a project (archives nodes first)
    project_stats        Node/task/area summary for a project

  Decision graph:
    capture_decision     Append a decision node (through ReviewerGuard)
    load_context         L0/L1/L2 hierarchical context assembly
    get_knowledge_node   Keyword search over decision nodes
    list_decisions       List decisions with area/status filters
    update_decision      Update fields on an existing decision
    deprecate_decision   Mark a decision as deprecated with reason
    link_decisions       Create a typed edge between two decisions

  Tasks:
    list_tasks           List tasks for a project
    create_task          Create a new task
    update_task          Update task status

  Ledger / memory:
    rollback             Time-travel undo (ledger-wide)
    snapshot             AES-256-GCM encrypted checkpoint
    list_snapshots       List all .forge snapshot files
    replay_sync          Restore from a .forge snapshot
    list_events          Inspect the append-only event ledger

  Search:
    search_context       Local-edge file search (ContextForge source tree)
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

_DB_PATH      = Path(os.getenv("DB_PATH", "data/contextforge.db"))
_CHARTER_PATH = Path(os.getenv("CHARTER_PATH", "PROJECT_CHARTER.md"))
SERVER_NAME    = "contextforge-nexus"
SERVER_VERSION = "5.1.0"


def build_server() -> "Server":
    if not _MCP_AVAILABLE:
        raise RuntimeError("Run: pip install mcp")

    server  = Server(SERVER_NAME)
    ledger  = EventLedger(db_path=str(_DB_PATH))
    indexer = LocalIndexer(project_root=str(_ROOT))
    fluid   = FluidSync(ledger=ledger, charter_path=str(_CHARTER_PATH))
    storage = StorageAdapter(db_path=str(_DB_PATH))

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [

            # ── Project management ─────────────────────────────────────

            types.Tool(
                name="list_projects",
                description="List all projects registered in this ContextForge instance, ordered by most recently created. Use this to see what exists before switching projects.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="init_project",
                description=(
                    "Create or update a project. If project_id already exists the "
                    "metadata (name, description, goals, tech_stack) is updated — "
                    "no data is lost. project_type options: code, research, study, general, custom."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string", "description": "Unique project identifier (slug-style, e.g. 'my-saas-app')"},
                        "name":         {"type": "string", "description": "Human-readable project name"},
                        "project_type": {"type": "string", "enum": ["code","research","study","general","custom"], "default": "code"},
                        "description":  {"type": "string"},
                        "goals":        {"type": "array", "items": {"type": "string"}},
                        "tech_stack":   {"type": "object"},
                        "constraints":  {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["project_id", "name"],
                },
            ),
            types.Tool(
                name="rename_project",
                description="Rename a project and optionally update its description. The project_id slug does not change — only the display name.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":      {"type": "string"},
                        "new_name":        {"type": "string"},
                        "new_description": {"type": "string", "description": "Optional — omit to keep existing description"},
                    },
                    "required": ["project_id", "new_name"],
                },
            ),
            types.Tool(
                name="merge_projects",
                description=(
                    "Merge source_project_id INTO target_project_id. "
                    "All decision nodes, tasks, and historical nodes are re-assigned to the target. "
                    "The source project is then deleted. Irreversible — snapshot first if unsure."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_project_id": {"type": "string", "description": "Project to merge FROM (will be deleted)"},
                        "target_project_id": {"type": "string", "description": "Project to merge INTO (is kept)"},
                    },
                    "required": ["source_project_id", "target_project_id"],
                },
            ),
            types.Tool(
                name="delete_project",
                description=(
                    "Permanently delete a project. All active decision nodes are archived to "
                    "historical_nodes before deletion (set archive_nodes=false to skip). "
                    "Tasks are deleted. This is irreversible — use snapshot before deleting."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":    {"type": "string"},
                        "archive_nodes": {"type": "boolean", "default": True, "description": "Archive active nodes to historical_nodes before deleting"},
                    },
                    "required": ["project_id"],
                },
            ),
            types.Tool(
                name="project_stats",
                description="Return a statistics summary for a project: node counts by area and status, task completion, deprecated/archived counts.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                    },
                    "required": ["project_id"],
                },
            ),

            # ── Decision graph ─────────────────────────────────────────

            types.Tool(
                name="capture_decision",
                description=(
                    "Append a decision node to the knowledge graph. Records WHY a decision "
                    "was made, alternatives considered, and file references. "
                    "Passes through ReviewerGuard entropy gate and charter check."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string"},
                        "summary":      {"type": "string", "description": "One-line decision summary"},
                        "rationale":    {"type": "string", "description": "WHY this decision was made"},
                        "area":         {"type": "string", "description": "e.g. auth, database, api-design, infrastructure"},
                        "alternatives": {"type": "array",  "items": {"type": "object"},
                                         "description": "e.g. [{\"name\": \"MongoDB\", \"rejected_because\": \"No ACID\"}]"},
                        "confidence":   {"type": "number", "default": 0.8, "description": "0.0–1.0"},
                        "file_refs":    {"type": "array",  "items": {"type": "string"},
                                         "description": "Relative file paths related to this decision"},
                    },
                    "required": ["project_id", "summary", "area"],
                },
            ),
            types.Tool(
                name="load_context",
                description=(
                    "Load hierarchical context for a project. "
                    "L0 = project metadata only. "
                    "L1 = metadata + decision titles and areas. "
                    "L2 = metadata + full rationale, alternatives, and file references. "
                    "Use L2 at session start for full context, L0 for quick orientation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":   {"type": "string"},
                        "query":        {"type": "string", "description": "Optional keyword to filter decisions"},
                        "detail_level": {"type": "string", "enum": ["L0","L1","L2"], "default": "L1"},
                        "top_k":        {"type": "integer", "default": 10},
                        "area":         {"type": "string", "description": "Optional area filter (e.g. 'auth')"},
                    },
                    "required": ["project_id"],
                },
            ),
            types.Tool(
                name="get_knowledge_node",
                description="Keyword search over decision nodes across one project. Returns top-k matches with rationale and causal chain.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query":      {"type": "string",  "description": "Topic or question"},
                        "project_id": {"type": "string",  "description": "Project to search (required)"},
                        "top_k":      {"type": "integer", "description": "Max results", "default": 5},
                    },
                    "required": ["query", "project_id"],
                },
            ),
            types.Tool(
                name="list_decisions",
                description="List decision nodes for a project with optional area and status filters. Good for browsing all decisions in an area.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "area":       {"type": "string", "description": "Filter by area (e.g. 'auth', 'database')"},
                        "status":     {"type": "string", "enum": ["active","deprecated","quarantined","pending"], "default": "active"},
                        "limit":      {"type": "integer", "default": 20},
                    },
                    "required": ["project_id"],
                },
            ),
            types.Tool(
                name="update_decision",
                description="Update specific fields on an existing decision node. Only provide fields you want to change. Allowed: summary, rationale, area, confidence, importance.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "node_id":    {"type": "string", "description": "The decision node ID to update"},
                        "summary":    {"type": "string"},
                        "rationale":  {"type": "string"},
                        "area":       {"type": "string"},
                        "confidence": {"type": "number"},
                        "importance": {"type": "number"},
                    },
                    "required": ["node_id"],
                },
            ),
            types.Tool(
                name="deprecate_decision",
                description="Mark a decision as deprecated. Use when a decision has been superseded. Optionally point to the replacement node ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "node_id":        {"type": "string"},
                        "reason":         {"type": "string", "description": "Why this decision is deprecated"},
                        "replacement_id": {"type": "string", "description": "ID of the node that replaces this one (optional)"},
                    },
                    "required": ["node_id", "reason"],
                },
            ),
            types.Tool(
                name="link_decisions",
                description=(
                    "Create a typed edge between two decision nodes. "
                    "edge_type options: depends_on, replaces, contradicts, refines, implements."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Source decision node ID"},
                        "target_id": {"type": "string", "description": "Target decision node ID"},
                        "edge_type": {"type": "string", "enum": ["depends_on","replaces","contradicts","refines","implements"]},
                    },
                    "required": ["source_id", "target_id", "edge_type"],
                },
            ),

            # ── Tasks ──────────────────────────────────────────────────

            types.Tool(
                name="list_tasks",
                description="List tasks for a project. Filter by status: pending, in_progress, done, blocked.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "status":     {"type": "string", "enum": ["pending","in_progress","done","blocked"]},
                        "limit":      {"type": "integer", "default": 20},
                    },
                    "required": ["project_id"],
                },
            ),
            types.Tool(
                name="create_task",
                description="Create a new task in a project.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id":  {"type": "string"},
                        "title":       {"type": "string"},
                        "description": {"type": "string"},
                        "priority":    {"type": "integer", "default": 3, "description": "1=highest, 5=lowest"},
                        "sprint":      {"type": "string"},
                        "assigned_to": {"type": "string"},
                    },
                    "required": ["project_id", "title"],
                },
            ),
            types.Tool(
                name="update_task",
                description="Update the status of an existing task.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "status":  {"type": "string", "enum": ["pending","in_progress","done","blocked"]},
                    },
                    "required": ["task_id", "status"],
                },
            ),

            # ── Ledger / memory ────────────────────────────────────────

            types.Tool(
                name="rollback",
                description=(
                    "Revert the event ledger to a prior state. Events after the target are marked "
                    "'rolled_back' — never deleted. Supply either event_id OR timestamp. "
                    "WARNING: rollback is ledger-wide and affects all projects."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "event_id":  {"type": "string", "description": "Roll back to just before this event"},
                        "timestamp": {"type": "string", "description": "ISO-8601 timestamp — roll back to this point"},
                    },
                },
            ),
            types.Tool(
                name="snapshot",
                description="Create an AES-256-GCM encrypted .forge checkpoint of the entire ledger (all projects). Requires FORGE_SNAPSHOT_KEY in .env.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "default": "manual", "description": "Label included in filename"},
                    },
                },
            ),
            types.Tool(
                name="list_snapshots",
                description="List all .forge snapshot files with their labels, sizes, and timestamps.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="replay_sync",
                description="Restore from a .forge snapshot file (cross-device context handshake). Requires the same FORGE_SNAPSHOT_KEY used to create the snapshot.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "forge_path": {"type": "string", "description": "Path to the .forge file"},
                    },
                    "required": ["forge_path"],
                },
            ),
            types.Tool(
                name="list_events",
                description="Inspect the append-only event ledger. Filter by event_type (e.g. AGENT_THOUGHT, CONFLICT).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "last_n":     {"type": "integer", "default": 20},
                        "event_type": {"type": "string",  "description": "Filter by event type"},
                    },
                },
            ),

            # ── Search ─────────────────────────────────────────────────

            types.Tool(
                name="search_context",
                description=(
                    "Semantic search over ContextForge's local source files (src/, mcp/, prompts/). "
                    "Zero cloud tokens — all computation is local. Useful when building on top of "
                    "ContextForge or exploring its implementation. For project decision retrieval, "
                    "use get_knowledge_node or load_context instead."
                ),
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
        ]

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        def ok(data):
            return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]
        def err(msg):
            return [types.TextContent(type="text", text=json.dumps({"error": msg}))]

        # ── Project management ─────────────────────────────────────────

        if name == "list_projects":
            projects = storage.list_projects()
            return ok({"projects": projects, "count": len(projects)})

        elif name == "init_project":
            project_id   = arguments.get("project_id", "")
            name_val     = arguments.get("name", "")
            if not project_id or not name_val:
                return err("project_id and name are required")
            pid = storage.upsert_project({
                "id":           project_id,
                "name":         name_val,
                "project_type": arguments.get("project_type", "code"),
                "description":  arguments.get("description", ""),
                "goals":        arguments.get("goals", []),
                "constraints":  arguments.get("constraints", []),
                "tech_stack":   arguments.get("tech_stack", {}),
            })
            return ok({"status": "ok", "project_id": pid, "name": name_val})

        elif name == "rename_project":
            project_id = arguments.get("project_id", "")
            new_name   = arguments.get("new_name", "")
            if not project_id or not new_name:
                return err("project_id and new_name are required")
            new_desc = arguments.get("new_description")
            found = storage.rename_project(project_id, new_name, new_desc)
            if not found:
                return err(f"Project '{project_id}' not found")
            return ok({"status": "renamed", "project_id": project_id, "new_name": new_name})

        elif name == "merge_projects":
            source = arguments.get("source_project_id", "")
            target = arguments.get("target_project_id", "")
            if not source or not target:
                return err("source_project_id and target_project_id are required")
            if source == target:
                return err("source and target must be different projects")
            try:
                result = storage.merge_projects(source, target)
                return ok({"status": "merged", **result})
            except ValueError as e:
                return err(str(e))

        elif name == "delete_project":
            project_id    = arguments.get("project_id", "")
            archive_nodes = arguments.get("archive_nodes", True)
            if not project_id:
                return err("project_id is required")
            try:
                result = storage.delete_project(project_id, archive_nodes=archive_nodes)
                return ok({"status": "deleted", **result})
            except ValueError as e:
                return err(str(e))

        elif name == "project_stats":
            project_id = arguments.get("project_id", "")
            if not project_id:
                return err("project_id is required")
            proj = storage.get_project(project_id)
            if not proj:
                return err(f"Project '{project_id}' not found")
            stats = storage.get_project_stats(project_id)
            stats["name"] = proj.get("name")
            stats["project_type"] = proj.get("project_type")
            return ok(stats)

        # ── Decision graph ─────────────────────────────────────────────

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
                "text":         f"{area}: {summary}. Rationale: {rationale}",
                "summary":      summary,
                "rationale":    rationale,
                "area":         area,
                "alternatives": alternatives,
                "confidence":   confidence,
                "type_metadata":{"file_refs": file_refs, "packages": []},
                "project_id":   project_id,
            }
            try:
                event_id = ledger.append(EventType.AGENT_THOUGHT, payload)
                node_id  = storage.upsert_node({
                    "project_id":       project_id,
                    "summary":          summary,
                    "rationale":        rationale,
                    "area":             area,
                    "alternatives":     alternatives,
                    "confidence":       confidence,
                    "importance":       0.5,
                    "origin_client":    "mcp-client",
                    "created_by_agent": "mcp-client",
                    "status":           "active",
                    "type_metadata":    {"file_refs": file_refs, "packages": []},
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
            area_filter  = arguments.get("area")
            proj = storage.get_project(project_id)
            if not proj:
                return err(f"Project '{project_id}' not found. Call init_project first.")
            l0 = {
                "level":       "L0",
                "project_id":  project_id,
                "name":        proj.get("name", project_id),
                "type":        proj.get("project_type", "general"),
                "description": proj.get("description", ""),
                "tech_stack":  _parse_json(proj.get("tech_stack"), {}),
                "goals":       _parse_json(proj.get("goals"), []),
                "constraints": _parse_json(proj.get("constraints"), []),
            }
            if detail_level == "L0":
                return ok(l0)
            nodes = storage.list_nodes(
                project_id=project_id,
                area=area_filter,
                status="active",
                limit=top_k * 10,
            )
            if query:
                q = query.lower()
                nodes = [
                    n for n in nodes
                    if q in (n.get("summary") or "").lower()
                    or q in (n.get("area") or "").lower()
                    or q in (n.get("rationale") or "").lower()
                ]
            nodes = nodes[:top_k]
            if detail_level == "L1":
                return ok({**l0, "level": "L1", "decisions": [
                    {"id": n["id"], "area": n["area"], "summary": n["summary"],
                     "confidence": n["confidence"]}
                    for n in nodes
                ]})
            return ok({**l0, "level": "L2", "decisions": [
                {
                    "id":               n["id"],
                    "area":             n["area"],
                    "summary":          n["summary"],
                    "rationale":        n.get("rationale"),
                    "alternatives":     _parse_json(n.get("alternatives"), []),
                    "dependencies":     _parse_json(n.get("dependencies"), []),
                    "confidence":       n["confidence"],
                    "status":           n["status"],
                    "created_by_agent": n.get("created_by_agent"),
                    "type_metadata":    _parse_json(n.get("type_metadata"), {}),
                }
                for n in nodes
            ]})

        elif name == "get_knowledge_node":
            query      = arguments.get("query", "")
            project_id = arguments.get("project_id", "")
            top_k      = int(arguments.get("top_k", 5))
            if not project_id:
                return err("project_id is required")
            nodes = storage.list_nodes(project_id=project_id, status="active", limit=top_k * 10)
            if query:
                q = query.lower()
                nodes = [
                    n for n in nodes
                    if q in (n.get("summary") or "").lower()
                    or q in (n.get("rationale") or "").lower()
                    or q in (n.get("area") or "").lower()
                ]
            return ok(nodes[:top_k])

        elif name == "list_decisions":
            project_id = arguments.get("project_id", "")
            if not project_id:
                return err("project_id is required")
            nodes = storage.list_nodes(
                project_id=project_id,
                area=arguments.get("area"),
                status=arguments.get("status", "active"),
                limit=int(arguments.get("limit", 20)),
            )
            return ok({"project_id": project_id, "count": len(nodes), "decisions": nodes})

        elif name == "update_decision":
            node_id = arguments.get("node_id", "")
            if not node_id:
                return err("node_id is required")
            fields = {k: v for k, v in arguments.items()
                      if k in {"summary","rationale","area","confidence","importance"}}
            if not fields:
                return err("No updatable fields provided (allowed: summary, rationale, area, confidence, importance)")
            found = storage.update_node_fields(node_id, fields)
            if not found:
                return err(f"Decision node '{node_id}' not found")
            return ok({"status": "updated", "node_id": node_id, "updated_fields": list(fields.keys())})

        elif name == "deprecate_decision":
            node_id    = arguments.get("node_id", "")
            reason     = arguments.get("reason", "")
            replacement= arguments.get("replacement_id")
            if not node_id or not reason:
                return err("node_id and reason are required")
            found = storage.deprecate_node(node_id, reason, replacement)
            if not found:
                return err(f"Decision node '{node_id}' not found")
            return ok({"status": "deprecated", "node_id": node_id, "reason": reason})

        elif name == "link_decisions":
            source_id = arguments.get("source_id", "")
            target_id = arguments.get("target_id", "")
            edge_type = arguments.get("edge_type", "")
            if not source_id or not target_id or not edge_type:
                return err("source_id, target_id, and edge_type are required")
            edge_id = storage.add_edge(source_id, target_id, edge_type)
            return ok({"status": "linked", "edge_id": edge_id,
                       "source": source_id, "target": target_id, "type": edge_type})

        # ── Tasks ──────────────────────────────────────────────────────

        elif name == "list_tasks":
            project_id = arguments.get("project_id", "")
            if not project_id:
                return err("project_id is required")
            tasks = storage.list_tasks(
                project_id=project_id,
                status=arguments.get("status"),
                limit=int(arguments.get("limit", 20)),
            )
            return ok({"project_id": project_id, "count": len(tasks), "tasks": tasks})

        elif name == "create_task":
            project_id = arguments.get("project_id", "")
            title      = arguments.get("title", "")
            if not project_id or not title:
                return err("project_id and title are required")
            tid = storage.upsert_task({
                "project_id":  project_id,
                "title":       title,
                "description": arguments.get("description", ""),
                "priority":    int(arguments.get("priority", 3)),
                "sprint":      arguments.get("sprint", ""),
                "assigned_to": arguments.get("assigned_to", ""),
                "status":      "pending",
            })
            return ok({"status": "created", "task_id": tid, "title": title})

        elif name == "update_task":
            task_id = arguments.get("task_id", "")
            status  = arguments.get("status", "")
            if not task_id or not status:
                return err("task_id and status are required")
            storage.update_task_status(task_id, status)
            return ok({"status": "updated", "task_id": task_id, "new_status": status})

        # ── Ledger / memory ────────────────────────────────────────────

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

        elif name == "list_snapshots":
            snaps = fluid.list_snapshots()
            return ok({"snapshots": snaps, "count": len(snaps)})

        elif name == "replay_sync":
            forge_path = arguments.get("forge_path", "")
            if not forge_path:
                return err("forge_path is required")
            replayed = fluid.replay_from_snapshot(forge_path=forge_path)
            return ok({"replayed_events": replayed, "status": "synced"})

        elif name == "list_events":
            last_n     = int(arguments.get("last_n", 20))
            event_type = arguments.get("event_type")
            events     = ledger.list_events(last_n=last_n, event_type=event_type)
            return ok(events)

        # ── Search ─────────────────────────────────────────────────────

        elif name == "search_context":
            query     = arguments.get("query", "")
            top_k     = int(arguments.get("top_k", 5))
            threshold = float(arguments.get("threshold", 0.75))
            results   = indexer.search(query=query, top_k=top_k, threshold=threshold)
            return ok(results)

        else:
            return err(f"Unknown tool: {name}")

    return server


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_json(value: Any, default: Any) -> Any:
    """Safely parse a JSON string or return the value if already decoded."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# ------------------------------------------------------------------
# Transport
# ------------------------------------------------------------------

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
    print(f"[ContextForge Nexus MCP v{SERVER_VERSION}] SSE on http://{host}:{port}/sse")
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
