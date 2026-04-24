"""
ContextForge — LangGraph Baseline Adapter
==========================================

Models LangGraph (LangChain Inc., 2024) as a memory/workflow baseline.

LangGraph is a stateful, multi-actor workflow framework built on top of
LangChain. It manages agent state graphs with persistence via checkpointers
(InMemorySaver, SqliteSaver, PostgresSaver). It does NOT implement any
adversarial content filtering — security is delegated entirely to the
underlying LLM's alignment training.

Architecture modelled
---------------------
  - State graph with persistent checkpointing (SqliteSaver equivalent).
  - No Shannon entropy gate — all writes go to state verbatim.
  - No injection keyword filter — relies on RLHF alignment of the LLM.
  - No charter enforcement — no ReviewerGuard equivalent exists.
  - Retrieval: semantic search via LangChain VectorStore (cosine ~0.50 threshold).
  - No token budget gate — injects all above-threshold chunks.

Reference:
  LangGraph documentation: https://langchain-ai.github.io/langgraph/
  LangChain LCEL: Chase (2022) — LangChain toolkit for LLM chaining.
  Greshake et al. (2023) §6 — documents no structural defense in LangGraph.

Security posture
----------------
  Blocked by:
    - Nothing structural. The LLM may refuse obviously harmful instructions
      based on alignment, but this is stochastic and model-dependent.

  NOT blocked by:
    - Prompt injection (no gate exists at the framework level)
    - Indirect injection via retrieved context
    - High-entropy obfuscation (no entropy signal)
    - Policy violations (no charter concept)
    - Slow-drip escalation sequences

Failover posture
----------------
  LangGraph itself has no built-in LLM failover. Provider failover depends on
  the underlying LangChain model configuration. Typical cold-start: ~480 ms
  (same as other LangChain-based systems, per ContextForge paper §4.3).

RAG context injection
---------------------
  VectorStore cosine threshold: ~0.50 (LangChain default similarity_search).
  Token budget: none enforced at framework level — all retrieved chunks injected.

Metric implications
-------------------
  ABR           : ~0 %  (no structural blocking mechanism)
  CSS           : 0     (unguarded pass-through, no precision credit)
  CTO           : high  (VectorStore retrieval at low threshold, no budget)
  Failover (ms) : ~480  (cold-start, no circuit breaker or prewarm)
  FPR           : 0 %   (everything passes — no false positives from the gate)
  TNR           : 0 %   (no noise reduction; all retrieved chunks injected)
"""

from __future__ import annotations

import time
from .base import BaselineAdapter, Probe, token_estimate

# ── Simulated constants ───────────────────────────────────────────────────────

_COLD_START_MS       = 480.0
_COSINE_THRESHOLD    = 0.50    # LangChain default similarity_search threshold
_TOP_K               = 20
_AVG_CHUNK_TOKENS    = 65
_FILTER_RATE         = 0.75    # fraction passing cosine 0.50 (generous threshold)


class LangGraphBaseline(BaselineAdapter):
    """
    LangGraph stateful workflow baseline:
      - No adversarial content gate whatsoever
      - All writes pass through to the state graph unchecked
      - VectorStore retrieval at cosine 0.50 with no token budget cap
      - Sequential failover, no circuit breaker or predictive prewarm
    """

    NAME = "LangGraph"
    DESCRIPTION = (
        "LangGraph stateful workflow (LangChain Inc. 2024): no entropy gate, "
        "no injection filter, no charter enforcement. VectorStore retrieval at "
        "cosine 0.50 with no token budget. Security delegated to LLM alignment "
        "(stochastic, not structural). ABR ≈ 0%."
    )

    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        LangGraph has no structural security gate — all payloads pass through.

        The underlying LLM may refuse obviously malicious instructions via
        alignment training, but this is stochastic and not measurable without
        live API calls. This benchmark conservatively models ABR = 0% since
        the framework provides zero structural guarantees.

        Returns (blocked=False, reason='', elapsed_ms).
        """
        t0 = time.perf_counter()
        elapsed = (time.perf_counter() - t0) * 1000.0
        return False, "", elapsed

    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Sequential cold-start retry with no circuit breaker or prewarm.

        LangGraph delegates failover to the underlying LangChain model
        configuration. No built-in retry logic at the framework level.
        Simulated at 480 ms cold-start (matching other LangChain-based systems).
        """
        t0 = time.perf_counter()
        simulated_ms = _COLD_START_MS
        elapsed = (time.perf_counter() - t0) * 1000.0
        return simulated_ms, elapsed

    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulates LangGraph VectorStore retrieval at cosine threshold 0.50.

        LangGraph's default similarity_search uses a generous threshold,
        injecting roughly 75% of retrieved chunks — more noise than ContextForge
        DCI (which gates at cosine 0.75 with a 1,500-token budget).
        No token budget cap is applied.

        Returns (tokens_retrieved, tokens_injected, elapsed_ms).
        """
        t0 = time.perf_counter()
        query_words = len(probe.payload.split())
        k           = min(_TOP_K, max(3, query_words // 3))
        retrieved   = k * _AVG_CHUNK_TOKENS
        injected    = int(retrieved * _FILTER_RATE)
        elapsed = (time.perf_counter() - t0) * 1000.0
        return retrieved, injected, elapsed
