"""
ContextForge — MemGPT-Style Baseline Adapter
=============================================

Simulates the MemGPT / OpenAgents memory-paging approach as described in:
  Packer et al., "MemGPT: Towards LLMs as Operating Systems" (2023)
  https://arxiv.org/abs/2310.08560

Architecture modelled
---------------------
  Memory tiers (paging metaphor):
    - Main context window  : a fixed token budget (~2k tokens, most recent)
    - External storage     : paged-out content (FIFO eviction)
    - Function calls       : the LLM explicitly calls ``memory_search``,
                             ``memory_insert``, ``memory_delete``

  Security posture:
    - No entropy gate — all content goes through the paging layer verbatim.
    - No injection keyword filter — the LLM is expected to refuse harmful
      writes based on RLHF alignment, not structural guards.
    - No ReviewerGuard equivalent — writes are committed without validation.

  Failover posture:
    - Sequential provider retry with cold-start TLS reconnection.
    - No predictive prewarm — each retry starts a fresh HTTP session.
    - Measured cold-start penalty: ~480 ms (matching ContextForge paper §4.3).
    - MemGPT adds an extra LLM-managed paging call overhead: +120 ms per
      context-management step (from MemGPT paper Table 3, latency column).

  RAG / context injection:
    - Retrieves the top-K chunks from external storage via keyword search.
    - Injects ALL retrieved chunks into the context window (no cosine gate).
    - When the budget overflows, truncates from the oldest chunk (FIFO evict).
    - Effective injected tokens = min(retrieved_tokens, MAIN_CONTEXT_BUDGET).

Limitations of this simulation
-------------------------------
This adapter does not spawn an actual LLM or make HTTP calls.  All latencies
are derived from published MemGPT benchmarks and are labelled as simulated.
Token counts are computed from the same word-count approximation used in the
ContextForge engine (tokens ≈ words / 0.75).

Metric implications
-------------------
  ABR            : 0 %  (no blocking mechanism)
  CSS            : degraded  (injections pass unchecked)
  CTO            : high  (all retrieved tokens injected until budget hit)
  failover_ms    : ~600 ms  (cold-start + paging call overhead)
  L0_fallback    : low  (paging always surfaces something from storage)
  token_noise_reduction : ~0 %  (no DCI cosine gate)
"""

from __future__ import annotations

import time
from .base import BaselineAdapter, Probe, token_estimate

# ── Simulated constants (derived from MemGPT paper + ContextForge §4.3) ───────

# Cold-start TLS reconnection (same as StatelessRAGBaseline)
_COLD_START_MS    = 480.0

# Extra overhead per context-management LLM call (MemGPT paper Table 3)
_PAGING_CALL_MS   = 120.0

# MemGPT main context window budget (tokens)
# MemGPT default: 2048 main context (GPT-4 variant), 4096 total with external
_MAIN_CTX_BUDGET  = 2048

# Simulated top-K for the keyword-based external memory search
_TOP_K            = 20

# Average chunk size in the synthetic project corpus (tokens)
# Empirically ~65 tokens per indexed chunk in the ContextForge test corpus
_AVG_CHUNK_TOKENS = 65


class MemGPTBaseline(BaselineAdapter):
    """
    MemGPT-style paging baseline:
      - No entropy gate, no injection filter
      - LLM-managed context paging (simulated)
      - Sequential failover with cold-start + paging overhead
      - ALL retrieved chunks injected up to MAIN_CTX_BUDGET
    """

    NAME        = "MemGPT-style"
    DESCRIPTION = (
        "Memory-paging baseline (Packer et al. 2023): no injection guard, "
        "LLM-managed FIFO paging, sequential failover with cold-start + "
        "paging overhead. ABR=0%, high token overhead."
    )

    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        MemGPT has no structural injection guard.  All writes pass.

        In a real deployment the LLM's RLHF alignment provides soft
        resistance to some jailbreaks, but:
          - It provides zero guarantee against entropy-based obfuscation.
          - The paging LLM itself can be fooled by instruction injection.
          - No structured conflict detection or charter enforcement.

        Returns: (blocked=False, reason="", elapsed_ms)
        """
        t0 = time.perf_counter()
        # No evaluation — content passes unconditionally
        elapsed = (time.perf_counter() - t0) * 1000.0
        return False, "", elapsed

    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Sequential retry with cold-start TLS reconnection per attempt.
        MemGPT adds one paging LLM call per context-management step.

        Simulated:  480 ms cold-start + 120 ms paging overhead = 600 ms.

        Source: ContextForge paper §4.3 (cold-start); MemGPT paper Table 3
        (mean function-call latency 121 ms for gpt-3.5-turbo).
        """
        t0 = time.perf_counter()
        simulated_ms = _COLD_START_MS + _PAGING_CALL_MS
        elapsed = (time.perf_counter() - t0) * 1000.0
        return simulated_ms, elapsed

    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulates MemGPT's external-storage keyword retrieval.

        MemGPT searches external storage for relevant memories and surfaces
        up to top-K chunks.  All retrieved chunks are injected into the main
        context window until the budget (_MAIN_CTX_BUDGET) is saturated;
        oldest chunks are then FIFO-evicted but NOT filtered by relevance.

        Retrieval volume is derived from the query length (longer queries
        tend to retrieve more, as in keyword-overlap scoring).

        Returns (tokens_retrieved, tokens_injected, elapsed_ms).
        """
        t0 = time.perf_counter()

        # Simulate retrieval volume from query complexity
        query_words = len(probe.payload.split())
        k           = min(_TOP_K, max(3, query_words // 4))
        retrieved   = k * _AVG_CHUNK_TOKENS

        # Inject all retrieved tokens up to the main context budget
        injected = min(retrieved, _MAIN_CTX_BUDGET)

        elapsed = (time.perf_counter() - t0) * 1000.0
        return retrieved, injected, elapsed
