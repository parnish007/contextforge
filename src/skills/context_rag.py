"""
ContextForge v3.0 — Context RAG Skill

Hierarchical context retrieval for the Coder (and any future) agent.

Tier priority (per OMEGA_SPEC §2.2 Hierarchical RAG):
  L1 — Librarian exact cache  (SHA-256 hit, near-zero latency)
  L2 — SQLite decision_nodes  (keyword BM25-style term overlap scoring)
  L3 — Research nodes         (area='research', most recent first)

Each tier assembles a ContextBundle and stores it back in the Librarian's
L1 cache so the same query is free on the next call.

Usage:
    rag = ContextRAG(librarian=librarian_agent, storage=storage_adapter)
    bundle = rag.retrieve(query="error handling decorator", project_id="proj-1")
    # bundle.content  → assembled context string
    # bundle.tier     → "L1" | "L2" | "L3" | "L0"
    # bundle.node_ids → node IDs included
"""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.agents.librarian import LibrarianAgent
    from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RAGBundle:
    """Result of a context retrieval pass."""
    content: str
    tier: str                              # "L1" | "L2" | "L3" | "L0"
    node_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # titles / summaries used
    token_estimate: int = 0


# ---------------------------------------------------------------------------
# Token / text helpers
# ---------------------------------------------------------------------------

def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token."""
    return max(1, len(text) // 4)


def _bm25_score(query_terms: list[str], text: str) -> float:
    """
    Simplified BM25 term-overlap score (no IDF, good enough for small corpora).
    Returns 0.0–1.0 normalised by query length.
    """
    if not query_terms:
        return 0.0
    text_lower = text.lower()
    tf: dict[str, int] = {}
    for term in query_terms:
        tf[term] = len(re.findall(re.escape(term.lower()), text_lower))
    hits = sum(1 for v in tf.values() if v > 0)
    # Saturate at log(1 + count) per term
    score = sum(math.log1p(v) for v in tf.values())
    coverage = hits / len(query_terms)
    return round((score / max(len(query_terms), 1)) * coverage, 4)


def _tokenise(query: str) -> list[str]:
    """Extract meaningful terms (length ≥ 3, stop-word stripped)."""
    _STOP = {
        "the", "and", "for", "with", "that", "this", "from", "are", "was",
        "not", "but", "all", "can", "how", "what", "use", "its", "our",
        "will", "has", "have", "been", "any", "more",
    }
    tokens = re.findall(r"[a-z][a-z0-9_]{2,}", query.lower())
    return [t for t in tokens if t not in _STOP]


# ---------------------------------------------------------------------------
# ContextRAG
# ---------------------------------------------------------------------------

MAX_L2_TOKENS = 2000
MAX_L3_TOKENS = 800
MAX_NODES = 6


class ContextRAG:
    """
    Hierarchical context retrieval skill.

    Parameters
    ----------
    librarian : LibrarianAgent | None
        Live Librarian agent for L1 cache lookups and put-backs.
    storage : StorageAdapter | None
        Direct StorageAdapter for L2/L3 SQLite reads when no Librarian.
    db_path : str
        Fallback SQLite path when neither librarian nor storage is wired.
    """

    def __init__(
        self,
        librarian: "LibrarianAgent | None" = None,
        storage: "StorageAdapter | None" = None,
        db_path: str = "data/contextforge.db",
    ):
        self._librarian = librarian
        self._storage = storage
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        project_id: str | None = None,
        max_tokens: int = MAX_L2_TOKENS + MAX_L3_TOKENS,
    ) -> RAGBundle:
        """
        Retrieve the best available context for `query`.

        Waterfall: L1 cache → L2 SQLite BM25 → L3 research nodes → L0 stub.
        """
        import hashlib
        cache_key = hashlib.sha256(query.strip().lower().encode()).hexdigest()

        # ── L1: Librarian in-process cache ────────────────────────────
        if self._librarian is not None:
            bundle = self._librarian.get(cache_key)
            if bundle:
                logger.info(
                    f"ContextRAG: L1 cache HIT  key={cache_key[:8]} "
                    f"tokens~={_approx_tokens(bundle.content)}"
                )
                return RAGBundle(
                    content=bundle.content,
                    tier="L1",
                    node_ids=bundle.node_ids,
                    token_estimate=_approx_tokens(bundle.content),
                )

        logger.debug(f"ContextRAG: L1 miss — querying L2 for '{query[:50]}'")

        # ── L2: SQLite decision_nodes keyword BM25 ────────────────────
        l2_bundle = self._retrieve_l2(query, project_id, max_tokens)
        if l2_bundle.node_ids:
            logger.info(
                f"ContextRAG: L2 hit — {len(l2_bundle.node_ids)} node(s) "
                f"tokens~={l2_bundle.token_estimate}"
            )
            # Store into L1 for next call
            if self._librarian is not None:
                self._librarian.put(
                    query, l2_bundle.content, "L2",
                    node_ids=l2_bundle.node_ids,
                )
            return l2_bundle

        # ── L3: Research nodes (area='research') ──────────────────────
        l3_bundle = self._retrieve_l3(query, project_id)
        if l3_bundle.node_ids:
            logger.info(
                f"ContextRAG: L3 research hit — {len(l3_bundle.node_ids)} node(s)"
            )
            if self._librarian is not None:
                self._librarian.put(
                    query, l3_bundle.content, "L3",
                    node_ids=l3_bundle.node_ids,
                )
            return l3_bundle

        # ── L0: Empty stub so the caller always gets something ─────────
        logger.warning(f"ContextRAG: all tiers empty for '{query[:50]}'")
        return RAGBundle(
            content=f"No prior context found for: {query}",
            tier="L0",
            token_estimate=10,
        )

    # ------------------------------------------------------------------
    # L2 — SQLite BM25
    # ------------------------------------------------------------------

    def _retrieve_l2(
        self, query: str, project_id: str | None, max_tokens: int
    ) -> RAGBundle:
        terms = _tokenise(query)
        rows = self._fetch_nodes(project_id, exclude_area="research", limit=40)
        if not rows:
            return RAGBundle(content="", tier="L2")

        # Score each node
        scored: list[tuple[float, dict]] = []
        for row in rows:
            text = " ".join(filter(None, [
                row.get("summary", ""),
                row.get("rationale", ""),
                row.get("area", ""),
            ]))
            score = _bm25_score(terms, text)
            if score > 0:
                scored.append((score, row))

        # Sort by score desc, then confidence desc as tiebreaker
        scored.sort(key=lambda x: (x[0], float(x[1].get("confidence") or 0)), reverse=True)
        top = scored[:MAX_NODES]

        if not top:
            return RAGBundle(content="", tier="L2")

        parts: list[str] = ["## Relevant Decision Nodes (L2)\n"]
        node_ids: list[str] = []
        sources: list[str] = []
        budget = max_tokens

        for score, node in top:
            block = _format_node(node)
            tok = _approx_tokens(block)
            if tok > budget:
                break
            parts.append(block)
            node_ids.append(node["id"])
            sources.append(node.get("summary", "")[:60])
            budget -= tok

        content = "\n".join(parts)
        return RAGBundle(
            content=content,
            tier="L2",
            node_ids=node_ids,
            sources=sources,
            token_estimate=_approx_tokens(content),
        )

    # ------------------------------------------------------------------
    # L3 — Research nodes
    # ------------------------------------------------------------------

    def _retrieve_l3(self, query: str, project_id: str | None) -> RAGBundle:
        rows = self._fetch_nodes(project_id, only_area="research", limit=10)
        if not rows:
            return RAGBundle(content="", tier="L3")

        terms = _tokenise(query)
        scored: list[tuple[float, dict]] = []
        for row in rows:
            text = row.get("summary", "") + " " + row.get("rationale", "")
            score = _bm25_score(terms, text) if terms else 1.0  # no terms → recency
            scored.append((score, row))

        scored.sort(key=lambda x: (x[0], x[1].get("created_at", "")), reverse=True)
        top = scored[:3]

        parts: list[str] = ["## Research Context (L3)\n"]
        node_ids: list[str] = []
        sources: list[str] = []
        budget = MAX_L3_TOKENS

        for _, node in top:
            block = _format_node(node, brief=True)
            tok = _approx_tokens(block)
            if tok > budget:
                break
            parts.append(block)
            node_ids.append(node["id"])
            sources.append(node.get("summary", "")[:60])
            budget -= tok

        content = "\n".join(parts)
        return RAGBundle(
            content=content,
            tier="L3",
            node_ids=node_ids,
            sources=sources,
            token_estimate=_approx_tokens(content),
        )

    # ------------------------------------------------------------------
    # SQLite fetch helper
    # ------------------------------------------------------------------

    def _fetch_nodes(
        self,
        project_id: str | None,
        exclude_area: str | None = None,
        only_area: str | None = None,
        limit: int = 40,
    ) -> list[dict]:
        """Read nodes from StorageAdapter or directly from SQLite."""
        if self._storage and project_id:
            try:
                nodes = self._storage.list_nodes(
                    project_id, area=only_area, status="active", limit=limit
                )
                if exclude_area:
                    nodes = [n for n in nodes if n.get("area") != exclude_area]
                return nodes
            except Exception as exc:
                logger.debug(f"ContextRAG: StorageAdapter read failed — {exc}")

        # Fallback: direct SQLite
        if not Path(self._db_path).exists():
            return []
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            query_parts = [
                "SELECT id, summary, rationale, area, confidence, type_metadata, created_at "
                "FROM decision_nodes WHERE tombstone=FALSE AND status='active'"
            ]
            params: list = []
            if project_id:
                query_parts.append("AND project_id=?")
                params.append(project_id)
            if only_area:
                query_parts.append("AND area=?")
                params.append(only_area)
            if exclude_area:
                query_parts.append("AND area!=?")
                params.append(exclude_area)
            query_parts.append("ORDER BY confidence DESC, created_at DESC LIMIT ?")
            params.append(limit)
            rows = conn.execute(" ".join(query_parts), params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"ContextRAG: direct SQLite read failed — {exc}")
            return []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_node(node: dict, brief: bool = False) -> str:
    nid = node.get("id", "?")[:8]
    summary = node.get("summary", "—")
    area = node.get("area", "general")
    conf = float(node.get("confidence") or 0)
    lines = [
        f"### [{area.upper()}] {summary}",
        f"id={nid}  conf={conf:.2f}",
    ]
    if not brief:
        rationale = node.get("rationale", "")
        if rationale:
            lines.append(f"Rationale: {rationale[:300]}")
    lines.append("")
    return "\n".join(lines)
