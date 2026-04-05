"""
ContextForge v3.0 — Historian Agent (Phase 4: Hardening)

Manages the Temporal Integrity of the SQLite knowledge graph.

Responsibilities:
  1. Find duplicate nodes (same area + agent + high lexical overlap)
     and archive older versions to historical_nodes, keeping only the
     most recent per "slot" — this keeps L2 RAG search fast and accurate.
  2. Tombstone nodes that are explicitly superseded by newer ones.
  3. Provide an audit trail via historical_nodes for any archived item.

Actions handled via reply():
  run_gc        — {"action": "run_gc"}                 full garbage-collect pass
  archive_node  — {"action": "archive_node", "node_id": str, "reason": str}
  list_history  — {"action": "list_history", "limit": int}
  get_stats     — {"action": "get_stats"}

Part of the ContextForge Nexus Architecture — Historian module.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

if TYPE_CHECKING:
    from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# Similarity helper (same cosine logic as Shadow-Reviewer)
# ---------------------------------------------------------------------------

def _term_freq(text: str) -> dict[str, int]:
    tokens = re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    return tf


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of term sets — lightweight duplicate detector."""
    sa = set(_term_freq(a))
    sb = set(_term_freq(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# HistorianAgent
# ---------------------------------------------------------------------------

class HistorianAgent(AgentBase):
    """
    Phase 4 — The Archivist.

    Parameters
    ----------
    name : str
    storage : StorageAdapter | None
        Shared database adapter.
    project_id : str | None
    duplicate_threshold : float
        Jaccard similarity above which two nodes are considered duplicates.
        Default 0.60.
    """

    def __init__(
        self,
        name: str = "Historian",
        storage: "StorageAdapter | None" = None,
        project_id: str | None = None,
        duplicate_threshold: float = 0.53,   # tuned in Omega Evolution Iter 5
    ):
        super().__init__()
        self.name = name
        self._storage = storage
        self._project_id = project_id or os.getenv("PROJECT_ID", "default")
        self._dup_threshold = duplicate_threshold
        logger.info(
            f"Historian initialised — project={self._project_id}, "
            f"dup_threshold={duplicate_threshold}"
        )

    # ------------------------------------------------------------------
    # AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        if x is None:
            return self._noop()
        payload: dict = {}
        if x.metadata and isinstance(x.metadata, dict):
            payload = x.metadata
        elif isinstance(x.content, str):
            try:
                payload = json.loads(x.content)
            except Exception:
                return self._noop()

        action = payload.get("action", "")
        if action == "run_gc":
            return self._handle_gc()
        if action == "archive_node":
            return self._handle_archive(payload)
        if action == "list_history":
            return self._handle_list_history(payload)
        if action == "get_stats":
            return self._handle_stats()
        return self._noop()

    # ------------------------------------------------------------------
    # Sync entry points
    # ------------------------------------------------------------------

    def run_gc(self) -> dict:
        """Synchronous GC pass — safe to call from main thread."""
        msg = self._handle_gc()
        return msg.metadata or {}

    def archive(self, node_id: str, reason: str = "manual") -> bool:
        if not self._storage:
            return False
        ok = self._storage.archive_node(node_id, reason=reason, archived_by=self.name)
        if ok:
            logger.info(f"Historian: archived node {node_id[:8]} — {reason}")
        return ok

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_gc(self) -> Msg:
        """Find and archive duplicate nodes, keeping only the newest per slot."""
        if not self._storage:
            return self._warn("no StorageAdapter wired")

        groups = self._storage.find_duplicates(self._project_id)
        archived_ids: list[str] = []
        slots_cleaned: int = 0

        for group in groups:
            # group is sorted newest-first; archive everything after index 0
            # but only if similarity to the newest is above threshold
            newest = group[0]
            for older in group[1:]:
                sim = _jaccard(
                    newest.get("summary", ""),
                    older.get("summary", ""),
                )
                if sim >= self._dup_threshold:
                    reason = (
                        f"Duplicate of {newest['id'][:8]} "
                        f"(jaccard={sim:.2f}) — kept newer version"
                    )
                    ok = self._storage.archive_node(
                        older["id"], reason=reason, archived_by=self.name
                    )
                    if ok:
                        archived_ids.append(older["id"])
            if archived_ids:
                slots_cleaned += 1

        summary = (
            f"Historian GC: {len(groups)} duplicate group(s) found, "
            f"{len(archived_ids)} node(s) archived, "
            f"{slots_cleaned} slot(s) cleaned"
        )
        logger.info(summary)
        return Msg(
            self.name,
            content=summary,
            role="assistant",
            metadata={
                "action": "gc_done",
                "groups_found": len(groups),
                "archived": len(archived_ids),
                "archived_ids": archived_ids,
            },
        )

    def _handle_archive(self, payload: dict) -> Msg:
        node_id = payload.get("node_id", "")
        reason = payload.get("reason", "manual archive")
        if not self._storage or not node_id:
            return self._warn("archive_node requires node_id and StorageAdapter")
        ok = self._storage.archive_node(node_id, reason=reason, archived_by=self.name)
        if ok:
            logger.info(f"Historian: archived {node_id[:8]} — {reason}")
        return Msg(
            self.name,
            content=f"Historian: {'archived' if ok else 'not found'} {node_id[:8]}",
            role="assistant",
            metadata={"action": "archive_done", "node_id": node_id, "success": ok},
        )

    def _handle_list_history(self, payload: dict) -> Msg:
        limit = int(payload.get("limit", 10))
        if not self._storage:
            return self._warn("no StorageAdapter")
        rows = self._storage.list_historical(self._project_id, limit=limit)
        return Msg(
            self.name,
            content=f"Historian: {len(rows)} historical record(s)",
            role="assistant",
            metadata={"action": "history_listed", "records": rows},
        )

    def _handle_stats(self) -> Msg:
        if not self._storage:
            return self._warn("no StorageAdapter")
        hist = self._storage.list_historical(self._project_id, limit=1000)
        groups = self._storage.find_duplicates(self._project_id)
        return Msg(
            self.name,
            content=f"Historian: {len(hist)} archived, {len(groups)} pending dups",
            role="assistant",
            metadata={
                "action": "stats",
                "archived_total": len(hist),
                "pending_duplicates": len(groups),
            },
        )

    # ------------------------------------------------------------------
    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})

    def _warn(self, msg: str) -> Msg:
        logger.warning(f"Historian: {msg}")
        return Msg(self.name, content=msg, role="assistant", metadata={"action": "warn", "detail": msg})
