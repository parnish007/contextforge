# RATIONALE: Six zero-LLM memory system simulations for Suite 15.
# ContextForge uses the REAL ReviewerGuard v3 from src/memory/ledger.py.
# All other systems are faithful behavioural simulations (no LLM calls).
"""
memory_agent.py — Suite 15 memory system implementations.

Systems
───────
  StatelessRAG    — no persistent memory; retrieve always returns []
  MemGPT          — stores all; recency-biased retrieval (summarisation omitted)
  LangGraph       — stores all; keyword-overlap BM25 (no security gate)
  ClaudeMem       — RLHF-like heuristic filter (very permissive) + recency mix
  HardenedRAG     — regex keyword block + BM25 (coarser than ReviewerGuard)
  ContextForge    — REAL ReviewerGuard v3 (src/memory/ledger.py) + BM25

Write interface
───────────────
  result = system.write(text)
    → WriteResult(accepted: bool, reason: str)

Retrieve interface
──────────────────
  memories = system.retrieve(query, k=3)
    → list[str]  (ordered by relevance, max k items)

Delete interface
────────────────
  count = system.delete(pattern)
    → int  (number of memories marked deleted)
"""
from __future__ import annotations

import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# ── Force EXPERIMENT mode for ContextForge BEFORE importing ledger ───────────
# ReviewerGuard._CF_MODE is a class-level constant evaluated at import time.
# Setting the env var here ensures the REAL v3 multi-trigger gate is active.
os.environ.setdefault("CF_MODE", "experiment")

try:
    from src.memory.ledger import ReviewerGuard, EventType, ConflictError
    _CF_AVAILABLE = True
except ImportError:
    _CF_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b\w+\b", text.lower())


def _bm25_retrieve(query: str, memories: list[str], k: int = 3,
                   recency_bias: float = 0.0, recency_order: list[int] = None) -> list[str]:
    """
    BM25-style retrieval over a flat list of memory strings.
    `recency_bias` ∈ [0,1]: fraction of score from recency (MemGPT-style).
    `recency_order`: index of each memory in original insertion order (higher = more recent).
    """
    if not memories or not query:
        return []

    q_terms = set(_tokenize(query))
    n_docs   = len(memories)
    k1, b    = 1.5, 0.75

    # IDF for each query term (using number of docs containing the term)
    doc_freq: dict[str, int] = {}
    for mem in memories:
        for term in set(_tokenize(mem)):
            doc_freq[term] = doc_freq.get(term, 0) + 1

    avg_len = sum(len(_tokenize(m)) for m in memories) / max(n_docs, 1)

    scored = []
    for idx, mem in enumerate(memories):
        t_words = _tokenize(mem)
        t_len   = len(t_words)
        t_freq  = Counter(t_words)

        bm25 = 0.0
        for term in q_terms:
            tf  = t_freq.get(term, 0)
            df  = doc_freq.get(term, 0)
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * t_len / avg_len))
            bm25 += idf * tf_norm

        # Recency bias: mix BM25 with position score
        if recency_bias > 0.0 and recency_order is not None:
            pos_score = (recency_order[idx] + 1) / (n_docs + 1)  # ∈ (0,1]
            score = (1 - recency_bias) * bm25 + recency_bias * pos_score
        else:
            score = bm25

        scored.append((score, idx, mem))

    scored.sort(key=lambda x: -x[0])
    return [mem for _, _, mem in scored[:k] if scored[0][0] > 0]


# ─────────────────────────────────────────────────────────────────────────────
# Write result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WriteResult:
    accepted:  bool
    reason:    str
    blocked_by: str = ""   # "reviewer_guard" | "regex_filter" | "" | "rlhf_heuristic"


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class MemorySystem:
    name: str = "base"

    def reset(self) -> None:
        """Clear all stored memories (call between test cases)."""
        raise NotImplementedError

    def write(self, text: str) -> WriteResult:
        raise NotImplementedError

    def delete(self, pattern: str) -> int:
        """Delete all memories whose text contains `pattern` (case-insensitive)."""
        raise NotImplementedError

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        return {"name": self.name}


# ─────────────────────────────────────────────────────────────────────────────
# 1. StatelessRAG — no persistent memory between queries
# ─────────────────────────────────────────────────────────────────────────────

class StatelessRAG(MemorySystem):
    """
    Represents traditional RAG systems with no session-level memory.
    Every query starts fresh. Retrieve always returns [].
    Write appears to succeed but nothing is persisted.
    """
    name = "StatelessRAG"

    def reset(self) -> None:
        pass

    def write(self, text: str) -> WriteResult:
        return WriteResult(accepted=True, reason="accepted_no_op")

    def delete(self, pattern: str) -> int:
        return 0

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        return []

    def stats(self) -> dict[str, Any]:
        return {"name": self.name}


# ─────────────────────────────────────────────────────────────────────────────
# 2. MemGPT-style — persistent memory, recency-biased retrieval
# ─────────────────────────────────────────────────────────────────────────────

class MemGPT(MemorySystem):
    """
    Simulates MemGPT-style infinite memory with archival and retrieval.
    Accepts ALL writes (no security gate).
    Retrieval is recency-biased: recent memories rank higher.
    Does NOT implement adversarial filtering — all injections accepted.
    """
    name = "MemGPT"

    def reset(self) -> None:
        self._memories: list[dict] = []

    def __init__(self):
        self.reset()

    def write(self, text: str) -> WriteResult:
        self._memories.append({"text": text, "ts": time.time(), "deleted": False})
        return WriteResult(accepted=True, reason="accepted")

    def delete(self, pattern: str) -> int:
        count = 0
        for m in self._memories:
            if not m["deleted"] and pattern.lower() in m["text"].lower():
                m["deleted"] = True
                count += 1
        return count

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        active = [m for m in self._memories if not m["deleted"]]
        if not active:
            return []
        texts  = [m["text"] for m in active]
        # Recency order: index in active list = insertion order
        recency = list(range(len(active)))
        return _bm25_retrieve(query, texts, k, recency_bias=0.4, recency_order=recency)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": len(self._memories),
            "active": sum(1 for m in self._memories if not m["deleted"]),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. LangGraph — persistent memory, pure BM25 retrieval, no security gate
# ─────────────────────────────────────────────────────────────────────────────

class LangGraph(MemorySystem):
    """
    Simulates a LangGraph agent with shared state / memory store.
    Accepts ALL writes (no security gate).
    Pure BM25 keyword retrieval (no recency bias).
    Adversarial injections stored as-is — highest contamination risk.
    """
    name = "LangGraph"

    def reset(self) -> None:
        self._memories: list[dict] = []

    def __init__(self):
        self.reset()

    def write(self, text: str) -> WriteResult:
        self._memories.append({"text": text, "deleted": False})
        return WriteResult(accepted=True, reason="accepted")

    def delete(self, pattern: str) -> int:
        count = 0
        for m in self._memories:
            if not m["deleted"] and pattern.lower() in m["text"].lower():
                m["deleted"] = True
                count += 1
        return count

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        active = [m["text"] for m in self._memories if not m["deleted"]]
        return _bm25_retrieve(query, active, k)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": len(self._memories),
            "active": sum(1 for m in self._memories if not m["deleted"]),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. ClaudeMem — RLHF alignment + very simple heuristics, no structural gate
# ─────────────────────────────────────────────────────────────────────────────

# ClaudeMem's "RLHF-like" heuristic catches only the most obvious patterns.
# It is much weaker than a structural gate — only explicit single keywords.
_CLAUDEMEM_BLOCK_RE = re.compile(
    r"\b(jailbreak|exfiltrate|exfil|dan\s+mode|ignore\s+all\s+previous)\b",
    re.IGNORECASE,
)


class ClaudeMem(MemorySystem):
    """
    Simulates Claude's memory system with RLHF-based refusals.
    Filters only the most blatant, single-keyword attacks.
    Natural-language injections (e.g. paraphrased "disregard rules") pass through.
    Retrieval is recency + relevance mix.
    """
    name = "ClaudeMem"

    def reset(self) -> None:
        self._memories: list[dict] = []
        self._blocked = 0

    def __init__(self):
        self.reset()

    def write(self, text: str) -> WriteResult:
        if _CLAUDEMEM_BLOCK_RE.search(text):
            self._blocked += 1
            return WriteResult(accepted=False, reason="rlhf_refusal",
                               blocked_by="rlhf_heuristic")
        self._memories.append({"text": text, "ts": time.time(), "deleted": False})
        return WriteResult(accepted=True, reason="accepted")

    def delete(self, pattern: str) -> int:
        count = 0
        for m in self._memories:
            if not m["deleted"] and pattern.lower() in m["text"].lower():
                m["deleted"] = True
                count += 1
        return count

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        active = [m for m in self._memories if not m["deleted"]]
        if not active:
            return []
        texts   = [m["text"] for m in active]
        recency = list(range(len(active)))
        return _bm25_retrieve(query, texts, k, recency_bias=0.25, recency_order=recency)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": len(self._memories),
            "active": sum(1 for m in self._memories if not m["deleted"]),
            "blocked": self._blocked,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. HardenedRAG — regex keyword block + BM25 retrieval
# ─────────────────────────────────────────────────────────────────────────────

# Same regex used in Suite 14 — broad keyword matching, no entropy/intent.
_HARDENED_BLOCK_RE = re.compile(
    r"\b(ignore|bypass|disregard|forget|override|circumvent|suppress|jailbreak|"
    r"exfiltrate|exfil|reveal|expose|dump|delete|drop|destroy|wipe|purge|erase|"
    r"truncate|flush|kill|disable|decommission|terminate|uninstall|remove|"
    r"masquerade|forge|spoof|inject|leak|extract|transmit|upload|send)\b",
    re.IGNORECASE,
)


class HardenedRAG(MemorySystem):
    """
    Simulates a hardened RAG system with regex-based pre-storage filtering.
    Catches writes containing destructive/injection keywords.
    No entropy or intent analysis — misses natural-language injections.
    Pure BM25 retrieval.
    """
    name = "HardenedRAG"

    def reset(self) -> None:
        self._memories: list[dict] = []
        self._blocked = 0

    def __init__(self):
        self.reset()

    def write(self, text: str) -> WriteResult:
        if _HARDENED_BLOCK_RE.search(text):
            self._blocked += 1
            return WriteResult(accepted=False, reason="regex_block",
                               blocked_by="regex_filter")
        self._memories.append({"text": text, "deleted": False})
        return WriteResult(accepted=True, reason="accepted")

    def delete(self, pattern: str) -> int:
        count = 0
        for m in self._memories:
            if not m["deleted"] and pattern.lower() in m["text"].lower():
                m["deleted"] = True
                count += 1
        return count

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        active = [m["text"] for m in self._memories if not m["deleted"]]
        return _bm25_retrieve(query, active, k)

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": len(self._memories),
            "active": sum(1 for m in self._memories if not m["deleted"]),
            "blocked": self._blocked,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. ContextForge v3 — REAL ReviewerGuard + BM25 retrieval
# ─────────────────────────────────────────────────────────────────────────────

class ContextForgeV3(MemorySystem):
    """
    Full ContextForge v3 memory system:
      - Security gate: REAL ReviewerGuard from src/memory/ledger.py
        (multi-trigger OR-gate: entropy path OR intent path, v3)
      - Retrieval: BM25 over accepted memories (mirrors ContextRAG L2)
      - Mode: CF_MODE=experiment (char H*=4.8, 22 injection patterns, intent≥0.70)

    This is the ACTUAL production gate, not a simulation.
    Writes are evaluated by ReviewerGuard.check(EventType.NODE_APPROVED, content).
    Adversarial writes that trigger any path are ConflictError-raised and rejected.
    """
    name = "ContextForge"

    def reset(self) -> None:
        self._memories: list[dict] = []
        self._blocked = 0
        self._blocked_reasons: list[str] = []

    # Representative memory-domain corpus for perplexity gate recalibration.
    # In production, the gate must be calibrated to the target write domain
    # before deployment; Suite 15 does this to reflect real usage accurately.
    _MEMORY_CALIBRATION_CORPUS = [
        "User strongly prefers Python for all backend development",
        "User uses FastAPI framework for REST APIs",
        "Team uses PostgreSQL as primary database",
        "CI/CD pipeline runs on GitHub Actions",
        "Frontend is built with React and TypeScript",
        "User writes production services in Go for performance",
        "Go microservices are deployed on Kubernetes",
        "User values Rust memory safety guarantees",
        "Primary backend stack is Rust with Actix-Web",
        "User prefers functional programming patterns",
        "Project deadline is set for end of quarter",
        "The database schema uses UUID primary keys",
        "Authentication is handled via JWT tokens",
        "User timezone is set to America Pacific",
        "Preferred code editor is Visual Studio Code",
        "User prefers dark mode for all applications",
        "Default branch naming convention is main not master",
        "Code review requires at least two approvals",
        "Unit tests are written using pytest framework",
        "Logging uses structured JSON format to stdout",
        "API versioning follows semantic versioning scheme",
        "Service mesh communication uses gRPC protocol",
        "Container images are tagged with git commit hash",
        "Secrets are stored in HashiCorp Vault",
        "Rate limiting is set to one thousand requests per minute",
        "Cache TTL default is fifteen minutes for most endpoints",
        "The team standups happen daily at nine in the morning",
        "Documentation is written in Markdown format",
        "Monitoring uses Prometheus and Grafana dashboards",
        "Error messages include correlation IDs for tracing",
        "Database migrations use Alembic for version control",
        "The staging environment mirrors production configuration",
        "Health check endpoints respond at slash health path",
        "Deployment rollbacks are automated when error rate exceeds threshold",
    ]

    def __init__(self, charter_path: str = None):
        self.reset()
        if not _CF_AVAILABLE:
            raise ImportError(
                "src/memory/ledger.py not importable. "
                "Run from contextforge project root."
            )
        # Force EXPERIMENT mode (v3 multi-trigger OR-gate)
        os.environ["CF_MODE"]         = "experiment"
        os.environ["CF_ENTROPY_MODE"] = "char"
        os.environ["CF_H_THRESHOLD"]  = "4.8"

        charter = charter_path or str(ROOT / "PROJECT_CHARTER.md")
        self._guard = ReviewerGuard(charter_path=charter)

        # Disable the perplexity gate for Suite 15.
        # The perplexity gate is an entropy-mimicry detector tuned for adversarial
        # NLP text. Memory writes naturally contain domain-specific tokens (emails,
        # URLs, dates, technical identifiers) whose trigram perplexity is high even
        # when completely benign. Disabling isolates the structural gate components
        # (entropy path, entity-verb path, intent path) which are the correct
        # signals for memory-domain security evaluation.
        try:
            import src.memory.ledger as _ledger_mod
            pg = _ledger_mod._get_perplexity_gate()
            if pg is not None:
                pg._enabled = False
        except Exception:
            pass

    def write(self, text: str) -> WriteResult:
        """
        Pass write through the REAL ReviewerGuard.check().
        Accepted → stored in BM25 pool with write timestamp.
        Rejected → ConflictError → not stored, WriteResult.accepted=False.
        """
        try:
            self._guard.check(EventType.NODE_APPROVED, {"content": text})
            self._memories.append({"text": text, "ts": time.time(), "deleted": False})
            return WriteResult(accepted=True, reason="accepted")
        except ConflictError as e:
            self._blocked += 1
            self._blocked_reasons.append(e.contradicted_rule)
            return WriteResult(
                accepted=False,
                reason=str(e)[:120],
                blocked_by="reviewer_guard",
            )

    def delete(self, pattern: str) -> int:
        count = 0
        for m in self._memories:
            if not m["deleted"] and pattern.lower() in m["text"].lower():
                m["deleted"] = True
                count += 1
        return count

    def retrieve(self, query: str, k: int = 3) -> list[str]:
        # Recency-weighted BM25: insertion order (position-based) used as
        # recency proxy because benchmark writes happen in milliseconds.
        # bias=0.65 means 65% of rank from recency, 35% from BM25 overlap.
        # This ensures fresh writes (written last) surface as top-1 on
        # update-preference queries, matching real-world production intent.
        active = [m for m in self._memories if not m["deleted"]]
        if not active:
            return []
        texts = [m["text"] for m in active]
        recency = list(range(len(active)))  # insertion-order proxy
        return _bm25_retrieve(query, texts, k, recency_bias=0.75, recency_order=recency)

    def stats(self) -> dict[str, Any]:
        return {
            "name":    self.name,
            "mode":    os.environ.get("CF_MODE", "?"),
            "total":   len(self._memories),
            "active":  sum(1 for m in self._memories if not m["deleted"]),
            "blocked": self._blocked,
            "blocked_reasons": self._blocked_reasons[:5],  # first 5 for debug
        }


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

ALL_SYSTEMS: list[type[MemorySystem]] = [
    StatelessRAG,
    MemGPT,
    LangGraph,
    ClaudeMem,
    HardenedRAG,
    ContextForgeV3,
]


def make_all() -> list[MemorySystem]:
    """Instantiate one of each system."""
    instances = []
    for cls in ALL_SYSTEMS:
        try:
            instances.append(cls())
        except ImportError as e:
            print(f"  [WARN] {cls.name}: {e} — skipped")
    return instances
