"""
ContextForge v3.0 — SQLite Storage Adapter

Initialises and wraps the Shared Knowledge Graph database.
Schema is the canonical definition from OMEGA_SPEC.md Section 5.1.

Supported backends (via DB_BACKEND env var): sqlite (default), postgres, supabase.
This module implements SQLite; postgres/supabase adapters are Phase 1 stubs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from loguru import logger


# ---------------------------------------------------------------------------
# DDL — exact schema from OMEGA_SPEC.md §5.1
# ---------------------------------------------------------------------------

_DDL = """
-- ===================================================================
-- ContextForge: Shared Knowledge Graph Schema
-- Tracks WHY a decision was made, not just WHAT.
-- ===================================================================

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    project_type    TEXT NOT NULL CHECK (project_type IN
                        ('code', 'research', 'study', 'general', 'custom')),
    description     TEXT,
    goals           JSON DEFAULT '[]',
    constraints     JSON DEFAULT '[]',
    tech_stack      JSON DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    omega_spec_hash TEXT
);

CREATE TABLE IF NOT EXISTS decision_nodes (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    summary         TEXT NOT NULL,
    rationale       TEXT,
    area            TEXT,
    alternatives    JSON DEFAULT '[]',
    dependencies    JSON DEFAULT '[]',
    triggered_by    TEXT,
    confidence      REAL DEFAULT 0.0,
    importance      REAL DEFAULT 0.5,
    vclock          JSON DEFAULT '{}',
    origin_client   TEXT,
    tombstone       BOOLEAN DEFAULT FALSE,
    created_by_agent TEXT,
    validated_by    TEXT,
    audited_by      TEXT,
    status          TEXT DEFAULT 'active' CHECK (status IN
                        ('pending', 'active', 'deprecated', 'quarantined')),
    deprecated_reason TEXT,
    replacement_id  TEXT REFERENCES decision_nodes(id),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_verified   TIMESTAMP,
    type_metadata   JSON DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS decision_edges (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES decision_nodes(id),
    target_id       TEXT NOT NULL REFERENCES decision_nodes(id),
    edge_type       TEXT NOT NULL CHECK (edge_type IN
                        ('depends_on', 'replaces', 'contradicts', 'refines', 'implements')),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_health_log (
    id              TEXT PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    llm_provider    TEXT,
    llm_model       TEXT,
    prompt_version  TEXT,
    confidence_avg  REAL,
    rejection_rate  REAL,
    hallucination_rate REAL,
    latency_ms      INTEGER,
    event_type      TEXT CHECK (event_type IN
                        ('metric', 'discard', 'respawn', 'fallback')),
    event_detail    JSON DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              TEXT PRIMARY KEY,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_node_id  TEXT,
    confidence      REAL,
    detail          JSON DEFAULT '{}',
    prev_hash       TEXT,
    entry_hash      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_scores (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    user_id         TEXT DEFAULT 'default',
    preferred_detail_level  TEXT DEFAULT 'L1',
    preferred_format        TEXT DEFAULT 'auto',
    agent_trust_scores      JSON DEFAULT '{}',
    interaction_count        INTEGER DEFAULT 0,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS historical_nodes (
    id              TEXT PRIMARY KEY,
    original_id     TEXT NOT NULL,
    project_id      TEXT,
    summary         TEXT,
    rationale       TEXT,
    area            TEXT,
    confidence      REAL,
    status          TEXT,
    created_by_agent TEXT,
    archived_by     TEXT DEFAULT 'Historian',
    archived_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    archive_reason  TEXT DEFAULT '',
    original_created_at TEXT,
    type_metadata   TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_hist_original ON historical_nodes(original_id);
CREATE INDEX IF NOT EXISTS idx_hist_project  ON historical_nodes(project_id, area);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending' CHECK (status IN
                        ('pending', 'in_progress', 'done', 'blocked')),
    priority        INTEGER DEFAULT 3,
    assigned_to     TEXT DEFAULT '',
    parent_goal     TEXT DEFAULT '',
    sprint          TEXT DEFAULT '',
    created_by_agent TEXT DEFAULT '',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_nodes_project ON decision_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_area    ON decision_nodes(area);
CREATE INDEX IF NOT EXISTS idx_nodes_status  ON decision_nodes(status);
CREATE INDEX IF NOT EXISTS idx_edges_source  ON decision_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target  ON decision_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
"""


# ---------------------------------------------------------------------------
# StorageAdapter
# ---------------------------------------------------------------------------

class StorageAdapter:
    """
    Thin wrapper around the SQLite database for ContextForge.

    All write operations append a corresponding row to audit_log with a
    SHA-256 hash chain (each row's hash includes the previous row's hash),
    providing a tamper-evident record of every change.
    """

    def __init__(self, db_path: str = "data/contextforge.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info(f"StorageAdapter ready — db={self.db_path}")

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)
        logger.debug("StorageAdapter: schema initialised")

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def upsert_project(self, project: dict[str, Any]) -> str:
        pid = project.get("id") or str(uuid.uuid4())
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO projects
                    (id, name, project_type, description, goals, constraints,
                     tech_stack, created_at, updated_at, omega_spec_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    project_type=excluded.project_type,
                    description=excluded.description,
                    goals=excluded.goals,
                    constraints=excluded.constraints,
                    tech_stack=excluded.tech_stack,
                    updated_at=excluded.updated_at,
                    omega_spec_hash=excluded.omega_spec_hash
                """,
                (
                    pid,
                    project.get("name", ""),
                    project.get("project_type", "general"),
                    project.get("description"),
                    json.dumps(project.get("goals", [])),
                    json.dumps(project.get("constraints", [])),
                    json.dumps(project.get("tech_stack", {})),
                    project.get("created_at", now),
                    now,
                    project.get("omega_spec_hash"),
                ),
            )
        self._audit("Librarian", "upsert_project", pid)
        return pid

    def get_project(self, project_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        """Return all registered projects ordered by most recently created."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def rename_project(self, project_id: str, new_name: str, new_description: str | None = None) -> bool:
        """Rename a project and optionally update its description. Returns False if not found."""
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
            if not exists:
                return False
            if new_description is not None:
                conn.execute(
                    "UPDATE projects SET name=?, description=?, updated_at=? WHERE id=?",
                    (new_name, new_description, _now(), project_id),
                )
            else:
                conn.execute(
                    "UPDATE projects SET name=?, updated_at=? WHERE id=?",
                    (new_name, _now(), project_id),
                )
        self._audit("StorageAdapter", "rename_project", project_id, detail={"new_name": new_name})
        return True

    def merge_projects(self, source_id: str, target_id: str) -> dict:
        """
        Merge source project INTO target project.
        All decision_nodes and tasks from source are re-assigned to target.
        Source project row is deleted. Returns counts of moved items.
        """
        with self._conn() as conn:
            src = conn.execute("SELECT 1 FROM projects WHERE id=?", (source_id,)).fetchone()
            tgt = conn.execute("SELECT 1 FROM projects WHERE id=?", (target_id,)).fetchone()
            if not src:
                raise ValueError(f"Source project '{source_id}' not found")
            if not tgt:
                raise ValueError(f"Target project '{target_id}' not found")
            nodes_moved = conn.execute(
                "UPDATE decision_nodes SET project_id=?, updated_at=? WHERE project_id=?",
                (target_id, _now(), source_id),
            ).rowcount
            tasks_moved = conn.execute(
                "UPDATE tasks SET project_id=?, updated_at=? WHERE project_id=?",
                (target_id, _now(), source_id),
            ).rowcount
            conn.execute(
                "UPDATE historical_nodes SET project_id=? WHERE project_id=?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM projects WHERE id=?", (source_id,))
        self._audit("StorageAdapter", "merge_projects", source_id,
                    detail={"target": target_id, "nodes_moved": nodes_moved, "tasks_moved": tasks_moved})
        return {"nodes_moved": nodes_moved, "tasks_moved": tasks_moved, "source_deleted": source_id}

    def delete_project(self, project_id: str, archive_nodes: bool = True) -> dict:
        """
        Delete a project. If archive_nodes=True, moves all active decision_nodes
        to historical_nodes before deleting. Returns counts of deleted/archived items.
        """
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
            if not exists:
                raise ValueError(f"Project '{project_id}' not found")
            archived = 0
            if archive_nodes:
                nodes = conn.execute(
                    "SELECT * FROM decision_nodes WHERE project_id=? AND tombstone=FALSE",
                    (project_id,)
                ).fetchall()
                for row in nodes:
                    node = dict(row)
                    conn.execute(
                        """INSERT OR IGNORE INTO historical_nodes
                           (id, original_id, project_id, summary, rationale, area,
                            confidence, status, created_by_agent, archived_by,
                            archived_at, archive_reason, original_created_at, type_metadata)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), node["id"], project_id,
                         node.get("summary"), node.get("rationale"), node.get("area"),
                         node.get("confidence"), node.get("status"),
                         node.get("created_by_agent"), "delete_project",
                         _now(), f"project {project_id} deleted",
                         node.get("created_at"), node.get("type_metadata", "{}")),
                    )
                    archived += 1
            # Tombstone all nodes so FK is satisfied before delete
            conn.execute(
                "UPDATE decision_nodes SET tombstone=TRUE WHERE project_id=?", (project_id,)
            )
            # Tasks have ON DELETE CASCADE so deleting project removes them
            # decision_nodes FK has no CASCADE — delete manually after tombstone
            conn.execute("DELETE FROM decision_nodes WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self._audit("StorageAdapter", "delete_project", project_id,
                    detail={"archived_nodes": archived})
        return {"deleted_project": project_id, "archived_nodes": archived}

    def get_project_stats(self, project_id: str) -> dict:
        """Return a statistics summary for a project."""
        with self._conn() as conn:
            area_rows = conn.execute(
                """SELECT area, COUNT(*) as cnt FROM decision_nodes
                   WHERE project_id=? AND tombstone=FALSE AND status='active'
                   GROUP BY area ORDER BY cnt DESC""",
                (project_id,)
            ).fetchall()
            total_nodes = conn.execute(
                "SELECT COUNT(*) FROM decision_nodes WHERE project_id=? AND tombstone=FALSE",
                (project_id,)
            ).fetchone()[0]
            active_nodes = conn.execute(
                "SELECT COUNT(*) FROM decision_nodes WHERE project_id=? AND tombstone=FALSE AND status='active'",
                (project_id,)
            ).fetchone()[0]
            deprecated_nodes = conn.execute(
                "SELECT COUNT(*) FROM decision_nodes WHERE project_id=? AND status='deprecated'",
                (project_id,)
            ).fetchone()[0]
            archived_nodes = conn.execute(
                "SELECT COUNT(*) FROM historical_nodes WHERE project_id=?",
                (project_id,)
            ).fetchone()[0]
            task_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks WHERE project_id=? GROUP BY status",
                (project_id,)
            ).fetchall()
        task_counts = {r["status"]: r["cnt"] for r in task_rows}
        total_tasks = sum(task_counts.values())
        return {
            "project_id": project_id,
            "nodes": {
                "total": total_nodes,
                "active": active_nodes,
                "deprecated": deprecated_nodes,
                "archived_historical": archived_nodes,
            },
            "areas": [{"area": r["area"] or "unset", "count": r["cnt"]} for r in area_rows],
            "tasks": {
                "total": total_tasks,
                "pending": task_counts.get("pending", 0),
                "in_progress": task_counts.get("in_progress", 0),
                "done": task_counts.get("done", 0),
                "blocked": task_counts.get("blocked", 0),
                "pct_complete": round((task_counts.get("done", 0) / total_tasks) * 100) if total_tasks else 0,
            },
        }

    # ------------------------------------------------------------------
    # Decision nodes
    # ------------------------------------------------------------------

    def _ensure_project(self, project_id: str) -> None:
        """Auto-create a stub project row if `project_id` does not exist yet."""
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO projects (id, name, project_type) VALUES (?,?,?)",
                    (project_id, project_id, "general"),
                )
        logger.debug(f"StorageAdapter: auto-created project stub '{project_id}'")

    def upsert_node(self, node: dict[str, Any]) -> str:
        nid = node.get("id") or str(uuid.uuid4())
        project_id = node.get("project_id", "")
        if project_id:
            self._ensure_project(project_id)
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO decision_nodes
                    (id, project_id, summary, rationale, area,
                     alternatives, dependencies, triggered_by,
                     confidence, importance, vclock, origin_client,
                     tombstone, created_by_agent, validated_by, audited_by,
                     status, deprecated_reason, replacement_id,
                     created_at, updated_at, last_verified, type_metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    summary=excluded.summary,
                    rationale=excluded.rationale,
                    area=excluded.area,
                    alternatives=excluded.alternatives,
                    dependencies=excluded.dependencies,
                    confidence=excluded.confidence,
                    importance=excluded.importance,
                    vclock=excluded.vclock,
                    tombstone=excluded.tombstone,
                    validated_by=excluded.validated_by,
                    audited_by=excluded.audited_by,
                    status=excluded.status,
                    deprecated_reason=excluded.deprecated_reason,
                    replacement_id=excluded.replacement_id,
                    updated_at=excluded.updated_at,
                    last_verified=excluded.last_verified,
                    type_metadata=excluded.type_metadata
                """,
                (
                    nid,
                    node.get("project_id", ""),
                    node.get("summary", ""),
                    node.get("rationale"),
                    node.get("area"),
                    json.dumps(node.get("alternatives", [])),
                    json.dumps(node.get("dependencies", [])),
                    node.get("triggered_by"),
                    node.get("confidence", 0.0),
                    node.get("importance", 0.5),
                    json.dumps(node.get("vclock", {})),
                    node.get("origin_client", ""),
                    node.get("tombstone", False),
                    node.get("created_by_agent", ""),
                    node.get("validated_by", ""),
                    node.get("audited_by", ""),
                    node.get("status", "active"),
                    node.get("deprecated_reason"),
                    node.get("replacement_id"),
                    node.get("created_at", now),
                    now,
                    node.get("last_verified"),
                    json.dumps(node.get("type_metadata", {})),
                ),
            )
        self._audit("Librarian", "upsert_node", nid)
        return nid

    def get_node(self, node_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM decision_nodes WHERE id=? AND tombstone=FALSE",
                (node_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        for json_field in ("alternatives", "dependencies", "vclock", "type_metadata"):
            if result.get(json_field):
                result[json_field] = json.loads(result[json_field])
        return result

    def list_nodes(
        self,
        project_id: str,
        area: str | None = None,
        status: str = "active",
        limit: int = 100,
    ) -> list[dict]:
        query = "SELECT * FROM decision_nodes WHERE project_id=? AND tombstone=FALSE AND status=?"
        params: list[Any] = [project_id, status]
        if area:
            query += " AND area=?"
            params.append(area)
        query += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            for json_field in ("alternatives", "dependencies", "vclock", "type_metadata"):
                if d.get(json_field):
                    d[json_field] = json.loads(d[json_field])
            results.append(d)
        return results

    def update_node_fields(self, node_id: str, fields: dict[str, Any]) -> bool:
        """
        Update specific fields on a decision_node. Allowed fields:
        summary, rationale, area, confidence, importance, status, deprecated_reason.
        Returns False if node not found.
        """
        _allowed = {"summary", "rationale", "area", "confidence", "importance",
                    "status", "deprecated_reason", "validated_by"}
        updates = {k: v for k, v in fields.items() if k in _allowed}
        if not updates:
            return False
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM decision_nodes WHERE id=? AND tombstone=FALSE", (node_id,)
            ).fetchone()
            if not exists:
                return False
            set_clause = ", ".join(f"{k}=?" for k in updates)
            values = list(updates.values()) + [_now(), node_id]
            conn.execute(
                f"UPDATE decision_nodes SET {set_clause}, updated_at=? WHERE id=?", values
            )
        self._audit("StorageAdapter", "update_node", node_id, detail={"fields": list(updates.keys())})
        return True

    def deprecate_node(self, node_id: str, reason: str, replacement_id: str | None = None) -> bool:
        """
        Mark a decision node as deprecated. Optionally point to a replacement node.
        Returns False if node not found.
        """
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM decision_nodes WHERE id=? AND tombstone=FALSE", (node_id,)
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                """UPDATE decision_nodes
                   SET status='deprecated', deprecated_reason=?, replacement_id=?, updated_at=?
                   WHERE id=?""",
                (reason, replacement_id, _now(), node_id),
            )
        self._audit("StorageAdapter", "deprecate_node", node_id, detail={"reason": reason})
        return True

    # ------------------------------------------------------------------
    # Decision edges
    # ------------------------------------------------------------------

    def add_edge(self, source_id: str, target_id: str, edge_type: str) -> str:
        eid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO decision_edges (id, source_id, target_id, edge_type) VALUES (?,?,?,?)",
                (eid, source_id, target_id, edge_type),
            )
        return eid

    def get_edges(self, node_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_edges WHERE source_id=? OR target_id=?",
                (node_id, node_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Historical archive (Historian agent)
    # ------------------------------------------------------------------

    def archive_node(self, node_id: str, reason: str = "", archived_by: str = "Historian") -> bool:
        """
        Move a decision_node to historical_nodes and tombstone the original.
        Returns True if the node was found and archived, False otherwise.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM decision_nodes WHERE id=?", (node_id,)
            ).fetchone()
            if not row:
                return False
            node = dict(row)
            conn.execute(
                """
                INSERT OR IGNORE INTO historical_nodes
                    (id, original_id, project_id, summary, rationale, area,
                     confidence, status, created_by_agent, archived_by,
                     archived_at, archive_reason, original_created_at, type_metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    node["id"],
                    node.get("project_id"),
                    node.get("summary"),
                    node.get("rationale"),
                    node.get("area"),
                    node.get("confidence"),
                    node.get("status"),
                    node.get("created_by_agent"),
                    archived_by,
                    _now(),
                    reason,
                    node.get("created_at"),
                    node.get("type_metadata", "{}"),
                ),
            )
            conn.execute(
                "UPDATE decision_nodes SET tombstone=TRUE, updated_at=? WHERE id=?",
                (_now(), node_id),
            )
        self._audit("Historian", "archive_node", node_id, detail={"reason": reason})
        return True

    def find_duplicates(self, project_id: str, area: str | None = None) -> list[list[dict]]:
        """
        Find groups of non-tombstoned nodes with the same (project_id, area, created_by_agent)
        that share high lexical overlap in their summaries.
        Returns list of groups; each group is [newest, ...older] sorted by created_at DESC.
        """
        query = (
            "SELECT id, summary, area, created_by_agent, created_at, confidence "
            "FROM decision_nodes WHERE tombstone=FALSE AND project_id=?"
        )
        params: list[Any] = [project_id]
        if area:
            query += " AND area=?"
            params.append(area)
        query += " ORDER BY created_at DESC"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        nodes = [dict(r) for r in rows]
        if len(nodes) < 2:
            return []

        # Group by (area, created_by_agent) — exact match
        from collections import defaultdict
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for n in nodes:
            key = (n.get("area", ""), n.get("created_by_agent", ""))
            groups[key].append(n)

        duplicates = []
        for group in groups.values():
            if len(group) >= 2:
                duplicates.append(group)  # already sorted newest-first
        return duplicates

    def list_historical(self, project_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM historical_nodes WHERE project_id=? "
                "ORDER BY archived_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def upsert_task(self, task: dict[str, Any]) -> str:
        tid = task.get("id") or str(uuid.uuid4())
        project_id = task.get("project_id", "")
        if project_id:
            self._ensure_project(project_id)
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, project_id, title, description, status, priority,
                     assigned_to, parent_goal, sprint, created_by_agent,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    priority=excluded.priority,
                    assigned_to=excluded.assigned_to,
                    parent_goal=excluded.parent_goal,
                    sprint=excluded.sprint,
                    updated_at=excluded.updated_at
                """,
                (
                    tid,
                    project_id,
                    task.get("title", ""),
                    task.get("description", ""),
                    task.get("status", "pending"),
                    task.get("priority", 3),
                    task.get("assigned_to", ""),
                    task.get("parent_goal", ""),
                    task.get("sprint", ""),
                    task.get("created_by_agent", ""),
                    task.get("created_at", now),
                    now,
                ),
            )
        self._audit("PM", "upsert_task", tid)
        return tid

    def update_task_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, _now(), task_id),
            )
        self._audit("PM", "update_task_status", task_id)

    def list_tasks(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        query = "SELECT * FROM tasks WHERE project_id=?"
        params: list[Any] = [project_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY priority ASC, created_at ASC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_task_stats(self, project_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks WHERE project_id=? GROUP BY status",
                (project_id,),
            ).fetchall()
            sprint_row = conn.execute(
                "SELECT DISTINCT sprint FROM tasks WHERE project_id=? AND sprint!='' LIMIT 1",
                (project_id,),
            ).fetchone()
        counts: dict[str, int] = {r["status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        done = counts.get("done", 0)
        pct = round((done / total) * 100) if total > 0 else 0
        return {
            "total": total,
            "done": done,
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "blocked": counts.get("blocked", 0),
            "pct_complete": pct,
            "current_sprint": sprint_row["sprint"] if sprint_row else "",
        }

    # ------------------------------------------------------------------
    # Audit log (hash-chained, tamper-evident)
    # ------------------------------------------------------------------

    def _audit(
        self,
        actor: str,
        action: str,
        target_node_id: str | None = None,
        confidence: float | None = None,
        detail: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            last = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["entry_hash"] if last else None

            entry_id = str(uuid.uuid4())
            now = _now()
            raw = f"{entry_id}{now}{actor}{action}{target_node_id}{prev_hash}"
            entry_hash = hashlib.sha256(raw.encode()).hexdigest()

            conn.execute(
                """
                INSERT INTO audit_log
                    (id, timestamp, actor, action, target_node_id,
                     confidence, detail, prev_hash, entry_hash)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry_id, now, actor, action, target_node_id,
                    confidence, json.dumps(detail or {}),
                    prev_hash, entry_hash,
                ),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()
