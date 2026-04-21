"""
ContextForge Nexus Architecture — JIT (Just-In-Time) Librarian
=======================================================

Bridge between the Local-Edge Speculative RAG indexer and the
existing AgentScope Librarian agent.

Responsibilities
────────────────
  1. Warm Cache Management
       Keeps a bounded LRU cache of recent query → result pairs so that
       repeated or near-identical lookups are returned instantly without
       re-scoring the embedding matrix.

  2. Pre-fetch on Query Arrival
       As soon as a user query is received, the JITLibrarian fires off a
       local search in the background (asyncio task) so that by the time
       the LLM call is ready, the relevant file chunks are already loaded.

  3. Differential Context Injection (DCI)
       Merges the LocalIndexer results (file-level) with the H-RAG results
       (graph-level) and deduplicates by chunk hash, giving the LLM a
       single unified context payload with zero redundancy.

  4. Token Budget Enforcement
       The merged payload is trimmed to fit within `token_budget` tokens,
       always prioritising higher-cosine chunks.

  5. Librarian Agent Notification
       After a successful index lookup, the JITLibrarian sends a
       `cache_hit` message to the Librarian agent so it can update its
       L1 cache state — keeping the two caches in sync.

Usage
─────
  from src.retrieval.jit_librarian import JITLibrarian

  jit = JITLibrarian(project_root=".", token_budget=1500)

  # Fire pre-fetch as soon as query arrives (non-blocking)
  jit.prefetch(query="JWT refresh token rotation")

  # Later — get the merged, token-budgeted context string
  context = await jit.get_context(query="JWT refresh token rotation")
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# Internal
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.retrieval.local_indexer import LocalIndexer
from src.config.dci_config import get_dci_config, CONTEXT_BUDGET_MODE


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class ContextChunk:
    """A single retrieved context fragment with provenance metadata."""
    source:    str        # file path or "graph:<node_id>"
    text:      str        # chunk text
    score:     float      # cosine similarity score (0.0–1.0)
    origin:    str        # "local_index" | "h_rag" | "l1_cache"
    chunk_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.chunk_hash = hashlib.sha256(self.text.encode()).hexdigest()[:16]

    def token_estimate(self) -> int:
        return max(1, int(len(self.text.split()) / 0.75))


@dataclass
class ContextPayload:
    """Unified context payload ready to inject into an LLM prompt."""
    chunks:       list[ContextChunk]
    total_tokens: int
    query:        str
    elapsed_ms:   float
    cache_hit:    bool
    sources:      list[str] = field(default_factory=list)

    def to_string(self) -> str:
        """Render chunks as a plain text context block for LLM injection."""
        if not self.chunks:
            return ""
        parts = ["=== Retrieved Context (Differential Injection) ==="]
        for i, chunk in enumerate(self.chunks, 1):
            parts.append(
                f"\n[{i}] Source: {chunk.source}  Score: {chunk.score:.3f}  "
                f"Origin: {chunk.origin}\n{chunk.text.strip()}"
            )
        parts.append(f"\n=== {len(self.chunks)} chunks · {self.total_tokens} tokens ===")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# LRU warm cache
# ---------------------------------------------------------------------------

class _LRUCache:
    """Thread-safe LRU cache with TTL support."""

    def __init__(self, maxsize: int = 128, ttl_seconds: float = 300.0) -> None:
        self._cache:   OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._maxsize  = maxsize
        self._ttl      = ttl_seconds
        self._hits     = 0
        self._misses   = 0

    def _key(self, query: str, top_k: int, threshold: float) -> str:
        return hashlib.sha256(f"{query}|{top_k}|{threshold:.3f}".encode()).hexdigest()[:32]

    def get(self, query: str, top_k: int, threshold: float) -> ContextPayload | None:
        key = self._key(query, top_k, threshold)
        if key not in self._cache:
            self._misses += 1
            return None
        payload, ts = self._cache[key]
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None
        # Move to end (most recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        return payload

    def put(self, query: str, top_k: int, threshold: float, payload: ContextPayload) -> None:
        key = self._key(query, top_k, threshold)
        self._cache[key] = (payload, time.monotonic())
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate_all(self) -> None:
        self._cache.clear()

    @property
    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits":      self._hits,
            "misses":    self._misses,
            "hit_rate":  round(self._hits / total, 4) if total else 0.0,
            "size":      len(self._cache),
            "maxsize":   self._maxsize,
        }


# ---------------------------------------------------------------------------
# JITLibrarian
# ---------------------------------------------------------------------------

class JITLibrarian:
    """
    Just-In-Time context assembler that merges local-edge file retrieval
    with graph-level H-RAG results, enforcing a token budget.

    Parameters
    ──────────
    project_root : str
        Root directory to index (default: current working directory).
    token_budget : int | None
        Maximum tokens for a context payload.  When None (default), the budget
        is resolved from ``src.config.dci_config`` using CONTEXT_BUDGET_MODE.
        Pass an explicit integer to override the config for this instance.
    threshold : float
        Minimum cosine similarity for a chunk to be included (default: 0.75).
    cache_maxsize : int
        Maximum LRU cache entries (default: 128).
    cache_ttl : float
        Seconds before a cache entry expires (default: 300).
    model_context_window : int | None
        Model context window in tokens — used when CONTEXT_BUDGET_MODE is
        "adaptive" or "model_aware" to compute B adaptively.
    model_name : str | None
        Model identifier string (e.g. "llama-3.3-70b-versatile") — used for
        automatic window lookup when model_context_window is not given.
    """

    def __init__(
        self,
        project_root:        str        = ".",
        token_budget:        int | None = None,
        threshold:           float      = 0.75,
        cache_maxsize:       int        = 128,
        cache_ttl:           float      = 300.0,
        model_context_window: int | None = None,
        model_name:          str | None = None,
    ) -> None:
        self._indexer      = LocalIndexer(project_root=project_root, threshold=threshold)
        self._cache        = _LRUCache(maxsize=cache_maxsize, ttl_seconds=cache_ttl)
        self._threshold    = threshold
        self._prefetch_tasks: dict[str, asyncio.Task] = {}

        # Resolve token budget via dci_config (unless overridden by caller)
        if token_budget is not None:
            self._token_budget = token_budget
            _budget_source     = f"explicit override: {token_budget}"
        else:
            dci = get_dci_config(
                model_context_window=model_context_window,
                model_name=model_name,
            )
            self._token_budget = dci.token_budget
            _budget_source     = dci.source

        # Librarian agent reference (set lazily via .attach_librarian())
        self._librarian: Any | None = None

        logger.info(
            f"[JITLibrarian] init  root={project_root}  "
            f"budget={self._token_budget}tok ({_budget_source})  "
            f"threshold={threshold}  mode={CONTEXT_BUDGET_MODE}"
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def attach_librarian(self, librarian: Any) -> None:
        """Attach the AgentScope LibrarianAgent for L1 cache notifications."""
        self._librarian = librarian
        logger.debug("[JITLibrarian] Librarian agent attached")

    def prefetch(self, query: str, top_k: int = 10) -> None:
        """
        Fire-and-forget: start a background asyncio task to pre-index and
        score the query so results are hot by the time get_context() is called.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return  # No event loop — skip prefetch in sync context

        if query in self._prefetch_tasks:
            return  # Already warming

        task = loop.create_task(
            self._prefetch_worker(query, top_k),
            name=f"jit-prefetch-{query[:20]}",
        )
        self._prefetch_tasks[query] = task
        task.add_done_callback(lambda t: self._prefetch_tasks.pop(query, None))
        logger.debug(f"[JITLibrarian] prefetch fired  query={query[:40]!r}")

    async def get_context(
        self,
        query:        str,
        top_k:        int   = 10,
        threshold:    float | None = None,
        h_rag_nodes:  list[dict] | None = None,
    ) -> ContextPayload:
        """
        Return a unified, token-budgeted context payload.

        Parameters
        ──────────
        query       : User query string.
        top_k       : Max chunks from local indexer.
        threshold   : Override cosine threshold for this call.
        h_rag_nodes : Optional list of H-RAG node dicts from LibrarianAgent.
                      Each dict must have 'summary' and 'rationale' keys.
        """
        t0        = time.monotonic()
        threshold = threshold if threshold is not None else self._threshold

        # 1. Check warm cache
        cached = self._cache.get(query, top_k, threshold)
        if cached:
            logger.debug(f"[JITLibrarian] cache HIT  query={query[:40]!r}")
            return dataclasses.replace(cached, cache_hit=True)

        # 2. Local-edge search (async, runs in thread to avoid blocking loop)
        local_hits = await asyncio.to_thread(
            self._indexer.search, query, top_k, threshold
        )
        local_chunks = [
            ContextChunk(
                source = hit.get("file_path", hit.get("file", "unknown")),
                text   = hit["text"],
                score  = hit["score"],
                origin = "local_index",
            )
            for hit in local_hits
        ]

        # 3. H-RAG nodes (already retrieved by caller)
        graph_chunks: list[ContextChunk] = []
        if h_rag_nodes:
            for node in h_rag_nodes:
                text = f"{node.get('summary', '')} {node.get('rationale', '')}".strip()
                if not text:
                    continue
                graph_chunks.append(ContextChunk(
                    source = f"graph:{node.get('id', 'unknown')[:8]}",
                    text   = text,
                    score  = float(node.get("confidence", 0.5)),
                    origin = "h_rag",
                ))

        # 4. Merge + deduplicate by chunk_hash
        all_chunks: list[ContextChunk] = []
        seen_hashes: set[str] = set()
        for chunk in sorted(
            local_chunks + graph_chunks,
            key=lambda c: c.score,
            reverse=True,
        ):
            if chunk.chunk_hash not in seen_hashes:
                all_chunks.append(chunk)
                seen_hashes.add(chunk.chunk_hash)

        # 5. Enforce token budget (greedy, highest-score first)
        selected:  list[ContextChunk] = []
        used_tokens = 0
        for chunk in all_chunks:
            est = chunk.token_estimate()
            if used_tokens + est > self._token_budget:
                continue
            selected.append(chunk)
            used_tokens += est

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        payload = ContextPayload(
            chunks       = selected,
            total_tokens = used_tokens,
            query        = query,
            elapsed_ms   = round(elapsed_ms, 2),
            cache_hit    = False,
            sources      = list({c.source for c in selected}),
        )

        # 6. Store in warm cache
        self._cache.put(query, top_k, threshold, payload)

        # 7. Notify Librarian agent (L1 cache sync)
        if self._librarian and selected:
            try:
                self._notify_librarian(query, payload)
            except Exception as exc:
                logger.debug(f"[JITLibrarian] Librarian notify failed: {exc}")

        logger.debug(
            f"[JITLibrarian] assembled  chunks={len(selected)}  "
            f"tokens={used_tokens}  elapsed={elapsed_ms:.1f}ms"
        )
        return payload

    def invalidate(self, file_path: str | None = None) -> None:
        """
        Invalidate the JIT cache.

        file_path : If given, only invalidate chunks from that file.
                    If None, clear the entire cache.
        """
        if file_path is None:
            self._cache.invalidate_all()
            self._indexer.invalidate_file.__func__  # check method exists
            logger.info("[JITLibrarian] Full cache invalidated")
        else:
            # Delegate file-level invalidation to the indexer
            self._indexer.invalidate_file(file_path)
            self._cache.invalidate_all()   # conservative: clear all entries
            logger.debug(f"[JITLibrarian] File invalidated: {file_path}")

    def rebuild_index(self) -> None:
        """Force a full re-index of the project (blocking)."""
        self._indexer.build_index(force=True)
        self._cache.invalidate_all()
        logger.info("[JITLibrarian] Index rebuilt")

    @property
    def stats(self) -> dict[str, Any]:
        """Return combined stats from indexer and cache."""
        idx_stats = self._indexer.stats()
        return {
            "cache":   self._cache.stats,
            "indexer": idx_stats,
        }

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _prefetch_worker(self, query: str, top_k: int) -> None:
        """Background task: pre-warm the cache with local index results."""
        try:
            await self.get_context(query, top_k=top_k)
        except Exception as exc:
            logger.debug(f"[JITLibrarian] prefetch error: {exc}")

    def _notify_librarian(self, query: str, payload: ContextPayload) -> None:
        """
        Notify the AgentScope Librarian of a cache hit so it can update L1.

        The Librarian's write_cache() method is called with a synthetic
        node dict constructed from the top chunk. This keeps L1 (in-process)
        and the JIT cache in sync without duplicating data.
        """
        if not payload.chunks:
            return
        top_chunk = payload.chunks[0]
        synthetic_node = {
            "id":       f"jit:{top_chunk.chunk_hash}",
            "summary":  top_chunk.text[:200],
            "rationale": f"# RATIONALE: JIT cache sync for query: {query[:80]}",
            "area":     "code",
            "confidence": top_chunk.score,
        }
        # LibrarianAgent exposes write_cache(node_dict) — call if available
        if hasattr(self._librarian, "write_cache"):
            self._librarian.write_cache(synthetic_node)


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_singleton: JITLibrarian | None = None


def get_jit_librarian(
    project_root:         str        = ".",
    token_budget:         int | None = None,
    threshold:            float      = 0.75,
    model_context_window: int | None = None,
    model_name:           str | None = None,
) -> JITLibrarian:
    """
    Return (or create) the process-level JITLibrarian singleton.

    Parameters
    ----------
    token_budget         : Explicit override; None = resolve from dci_config.
    model_context_window : Window size for adaptive/model_aware budget modes.
    model_name           : Model string for automatic window lookup.
    """
    global _singleton
    if _singleton is None:
        _singleton = JITLibrarian(
            project_root         = project_root,
            token_budget         = token_budget,
            threshold            = threshold,
            model_context_window = model_context_window,
            model_name           = model_name,
        )
    return _singleton
