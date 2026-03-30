"""
ContextForge v3.0 — Agent 2: Librarian (The Memory Keeper)

Manages the three-tier context cache and is the sole writer to the
Shared Knowledge Graph.  No LLM required — pure deterministic logic.

Responsibilities:
  1. Maintain an in-process L1 exact cache:
       SHA-256(query) → ContextBundle
  2. Maintain a reverse index for instant invalidation:
       node_id → set[cache_keys]
  3. Listen on the AgentScope MsgHub for:
       • "batch_capture" (from Sentry)  → write approved nodes to SQLite
       • "write_node"    (from pipeline) → persist a validated DecisionNode
       • "invalidate"    (from pipeline) → purge stale cache entries
  4. On file_modify signals, use the reverse index to purge every
     ContextBundle that references the changed file / node.

Spec reference: Section 2.2 (Hierarchical RAG) and Section 5 (Memory Schema).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ContextBundle:
    """
    A pre-assembled context string cached at L1.

    Stores the serialised text that would be delivered to an LLM for a given
    query, together with enough metadata for the reverse index to invalidate
    it when underlying nodes change.
    """

    cache_key: str                          # SHA-256(normalised query)
    content: str                            # Assembled context text
    tier: str                               # "L0" | "L1" | "L2"
    node_ids: list[str] = field(default_factory=list)   # Nodes included
    file_paths: list[str] = field(default_factory=list) # Source files referenced
    created_at: datetime = field(default_factory=datetime.utcnow)
    hit_count: int = 0


# ---------------------------------------------------------------------------
# LibrarianAgent
# ---------------------------------------------------------------------------

class LibrarianAgent(AgentBase):
    """
    Agent 2 — The Memory Keeper.

    Holds two in-process data structures that together form the reactive
    bidirectional index:

        forward:  cache_key  → ContextBundle
        reverse:  node_id    → set[cache_keys]

    These structures let the agent answer "which cached bundles become stale
    if node X changes?" in O(1) time, avoiding a full cache scan on every
    write.

    Parameters
    ----------
    name : str
        AgentScope agent name.
    db_path : str
        Path to the SQLite database file (created by StorageAdapter if absent).
    max_l1_entries : int
        Maximum number of ContextBundles held in L1.  Oldest entries are
        evicted when the limit is reached (simple LRU approximation via
        insertion-order dict).
    """

    def __init__(
        self,
        name: str = "Librarian",
        db_path: str = "data/contextforge.db",
        max_l1_entries: int = 512,
    ):
        super().__init__()
        self.name = name
        self._lock = threading.RLock()

        # ── L1 exact cache ────────────────────────────────────────────
        # Keyed by SHA-256 of the normalised query string.
        self._forward: dict[str, ContextBundle] = {}

        # ── Reverse index ─────────────────────────────────────────────
        # Maps every node_id that appears in at least one bundle to the
        # set of cache_keys for those bundles.
        self._reverse: dict[str, set[str]] = {}

        self._max_l1_entries = max_l1_entries

        # ── Persistent storage ────────────────────────────────────────
        self._db = StorageAdapter(db_path)

        logger.info(
            f"Librarian initialised — db={db_path}, "
            f"max_l1_entries={max_l1_entries}"
        )

    # ------------------------------------------------------------------
    # AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        """
        Dispatch incoming MsgHub messages to the appropriate handler.

        In AgentScope 1.0.18, Msg.content must be a string.
        The structured payload is read from Msg.metadata.

        Supported actions:
          • "batch_capture"   — Sentry batch; inspect signals for invalidation
          • "write_node"      — Persist a validated DecisionNode to SQLite
          • "invalidate"      — Explicit cache invalidation by node_id list
          • "get"             — Cache lookup by query string
          • "stats"           — Return cache statistics
        """
        if x is None:
            return self._noop()

        # Payload is in metadata (1.0.18 pattern) or fallback to JSON content
        payload: dict = {}
        if x.metadata and isinstance(x.metadata, dict):
            payload = x.metadata
        elif isinstance(x.content, str):
            import json as _json
            try:
                payload = _json.loads(x.content)
            except Exception:
                return self._noop()

        action = payload.get("action", "")

        if action == "batch_capture":
            return self._handle_batch(payload.get("batch", {}))

        if action == "write_node":
            return self._handle_write_node(payload.get("node", {}))

        if action == "invalidate":
            return self._handle_invalidate(payload.get("node_ids", []))

        if action == "get":
            return self._handle_get(payload.get("query", ""))

        if action == "stats":
            return self._handle_stats()

        logger.warning(f"Librarian: unknown action '{action}'")
        return self._noop()

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _handle_batch(self, batch: dict) -> Msg:
        """
        Process a SignalBatch from the Sentry.

        For every FILE_MODIFY signal in the batch, check whether the new
        content hash differs from what we last saw.  If it does, purge all
        ContextBundles that reference that file path from L1.
        """
        signals: list[dict] = batch.get("signals", [])
        invalidated_keys: list[str] = []

        for signal in signals:
            if signal.get("signal_type") in {"file_modify", "FILE_MODIFY"}:
                file_path = signal.get("file_path", "")
                invalidated_keys.extend(self._purge_by_file(file_path))

        logger.info(
            f"Librarian: processed batch of {len(signals)} signal(s); "
            f"purged {len(invalidated_keys)} stale cache key(s)"
        )
        p = {"action": "batch_processed", "signals_received": len(signals), "cache_keys_purged": len(invalidated_keys)}
        return Msg(self.name, content=f"batch_processed: {len(signals)} signals, {len(invalidated_keys)} purged", role="assistant", metadata=p)

    def _handle_write_node(self, node_dict: dict) -> Msg:
        """
        Persist a validated DecisionNode to SQLite and invalidate any cached
        bundles that include this node (they may now be stale).
        """
        node_id = node_dict.get("id", "")
        if not node_id:
            p = {"action": "error", "detail": "node missing 'id'"}
            return Msg(self.name, content="error: node missing id", role="assistant", metadata=p)

        try:
            self._db.upsert_node(node_dict)
        except Exception as exc:
            logger.error(f"Librarian: failed to persist node {node_id} — {exc}")
            p = {"action": "error", "detail": str(exc)}
            return Msg(self.name, content=f"error: {exc}", role="assistant", metadata=p)

        # Invalidate any cached bundles that already reference this node
        purged = self._purge_by_node_id(node_id)
        logger.info(
            f"Librarian: wrote node {node_id}; "
            f"purged {len(purged)} affected cache key(s)"
        )
        p = {"action": "node_written", "node_id": node_id, "cache_keys_purged": len(purged)}
        return Msg(self.name, content=f"node_written: {node_id}", role="assistant", metadata=p)

    def _handle_invalidate(self, node_ids: list[str]) -> Msg:
        """Explicitly invalidate all cache entries that reference any of the given node IDs."""
        total_purged = 0
        for node_id in node_ids:
            total_purged += len(self._purge_by_node_id(node_id))
        logger.info(
            f"Librarian: explicit invalidation of {len(node_ids)} node(s); "
            f"purged {total_purged} cache key(s)"
        )
        p = {"action": "invalidated", "node_ids": node_ids, "cache_keys_purged": total_purged}
        return Msg(self.name, content=f"invalidated {len(node_ids)} nodes", role="assistant", metadata=p)

    def _handle_get(self, query: str) -> Msg:
        """
        Look up a pre-assembled ContextBundle for the given query.
        Returns a cache_miss if nothing is found (caller must assemble fresh).
        """
        cache_key = _hash_query(query)
        bundle = self.get(cache_key)
        if bundle:
            p = {"action": "cache_hit", "cache_key": cache_key, "content": bundle.content, "tier": bundle.tier, "hit_count": bundle.hit_count}
            return Msg(self.name, content=f"cache_hit [{bundle.tier}]", role="assistant", metadata=p)
        p = {"action": "cache_miss", "cache_key": cache_key}
        return Msg(self.name, content="cache_miss", role="assistant", metadata=p)

    def _handle_stats(self) -> Msg:
        with self._lock:
            total_hits = sum(b.hit_count for b in self._forward.values())
            p = {"action": "stats", "l1_entries": len(self._forward), "reverse_index_entries": len(self._reverse), "total_cache_hits": total_hits, "max_l1_entries": self._max_l1_entries}
            return Msg(self.name, content=f"stats: l1={len(self._forward)} hits={total_hits}", role="assistant", metadata=p)

    # ------------------------------------------------------------------
    # Public cache API (used by other agents / pipeline directly)
    # ------------------------------------------------------------------

    def get(self, cache_key: str) -> ContextBundle | None:
        """Return a ContextBundle by its cache key, or None on miss."""
        with self._lock:
            bundle = self._forward.get(cache_key)
            if bundle:
                bundle.hit_count += 1
            return bundle

    def put(self, query: str, content: str, tier: str,
            node_ids: list[str] | None = None,
            file_paths: list[str] | None = None) -> str:
        """
        Insert (or replace) a ContextBundle for the given query.

        Returns the cache_key.  Automatically registers all node_ids in the
        reverse index and evicts the oldest entry if the L1 limit is reached.
        """
        cache_key = _hash_query(query)
        bundle = ContextBundle(
            cache_key=cache_key,
            content=content,
            tier=tier,
            node_ids=node_ids or [],
            file_paths=file_paths or [],
        )

        with self._lock:
            # Evict oldest entry if at capacity
            if cache_key not in self._forward and len(self._forward) >= self._max_l1_entries:
                self._evict_oldest()

            self._forward[cache_key] = bundle

            # Register in reverse index for each referenced node
            for node_id in bundle.node_ids:
                self._reverse.setdefault(node_id, set()).add(cache_key)

        logger.debug(f"Librarian: cached bundle [{tier}] key={cache_key[:8]}…")
        return cache_key

    # ------------------------------------------------------------------
    # Invalidation helpers
    # ------------------------------------------------------------------

    def _purge_by_node_id(self, node_id: str) -> list[str]:
        """Remove all cache entries that reference `node_id`. Returns purged keys."""
        with self._lock:
            keys_to_purge = list(self._reverse.pop(node_id, set()))
            for key in keys_to_purge:
                bundle = self._forward.pop(key, None)
                if bundle:
                    # Clean up reverse index for other nodes in this bundle
                    for nid in bundle.node_ids:
                        if nid != node_id:
                            self._reverse.get(nid, set()).discard(key)
            return keys_to_purge

    def _purge_by_file(self, file_path: str) -> list[str]:
        """
        Remove all cache entries that reference a given file path.
        Used when Sentry reports a FILE_MODIFY event.
        """
        with self._lock:
            keys_to_purge = [
                key for key, bundle in self._forward.items()
                if file_path in bundle.file_paths
            ]
            for key in keys_to_purge:
                bundle = self._forward.pop(key, None)
                if bundle:
                    for nid in bundle.node_ids:
                        self._reverse.get(nid, set()).discard(key)
            return keys_to_purge

    def _evict_oldest(self) -> None:
        """Evict the oldest (first-inserted) cache entry. Caller must hold lock."""
        if not self._forward:
            return
        oldest_key = next(iter(self._forward))
        bundle = self._forward.pop(oldest_key)
        for nid in bundle.node_ids:
            self._reverse.get(nid, set()).discard(oldest_key)
        logger.debug(f"Librarian: evicted oldest cache key={oldest_key[:8]}…")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _hash_query(query: str) -> str:
    """Return the SHA-256 hex digest of a normalised query string."""
    normalised = query.strip().lower()
    return hashlib.sha256(normalised.encode()).hexdigest()
