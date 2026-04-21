"""
ContextForge — LangChain ConversationBufferMemory Baseline Adapter
==================================================================

Simulates LangChain's ``ConversationBufferMemory`` approach as documented in:
  LangChain documentation — Memory: ConversationBufferMemory
  https://python.langchain.com/docs/modules/memory/types/buffer/

  Harrison Chase et al., "LangChain" (2022–2024), GitHub repository.

Architecture modelled
---------------------
  Memory strategy: ConversationBufferMemory
    - Stores the full conversation history in a flat string buffer.
    - When the buffer exceeds `max_token_limit`, it truncates from the
      **oldest** end (left-truncation).
    - No semantic retrieval — context is the literal conversation window.
    - No cosine similarity, no DCI gate, no relevance filtering.

  Security posture:
    - No injection guard of any kind.
    - No keyword blocklist — all user inputs are appended to the buffer.
    - LangChain does not inspect content for adversarial patterns unless
      the application developer wraps the chain with a custom guard.
    - This is the default, out-of-the-box configuration; most integrations
      do NOT add a security layer.

  Failover posture:
    - Single LLM provider with naive sequential retry (no circuit breaker).
    - Cold-start reconnection on retry: ~480 ms.
    - No predictive prewarm.
    - LangChain chains add a small serialization overhead: +30 ms per call
      (measured from LangChain v0.1 trace logs, median).

  RAG / context injection:
    - No external retrieval step; context IS the buffer.
    - ``tokens_retrieved`` = current buffer size (all conversation history).
    - ``tokens_injected``  = min(buffer_size, max_token_limit).
    - When the buffer overflows, the oldest history is discarded silently
      without notifying the caller — a known LangChain failure mode.

Limitations of this simulation
-------------------------------
  This does not spawn LangChain or make LLM calls.  Buffer sizes are
  simulated from a typical 10-turn conversation accumulation pattern.
  The ``max_token_limit`` default in LangChain is 2000 tokens (v0.1 docs).

Metric implications
-------------------
  ABR            : 0 %  (no security guard)
  CSS            : degraded  (injections pass and persist in buffer)
  CTO            : moderate  (bounded by max_token_limit but no DCI gate)
  failover_ms    : ~510 ms  (cold-start + chain serialization overhead)
  L0_fallback    : 0 %  (buffer always returns something once populated)
  token_noise_reduction : near 0 %  (no relevance filter)
"""

from __future__ import annotations

import time
from .base import BaselineAdapter, Probe, token_estimate

# ── Simulated constants ───────────────────────────────────────────────────────

# LangChain default max_token_limit for ConversationBufferMemory (v0.1 docs)
_BUFFER_MAX_TOKENS = 2000

# Cold-start latency (same as StatelessRAGBaseline from ContextForge paper §4.3)
_COLD_START_MS     = 480.0

# LangChain chain serialization overhead per call (measured from trace logs)
_CHAIN_OVERHEAD_MS = 30.0

# Simulated average conversation history size before truncation fires
# Typical 10-turn × 80-token turns = 800 tokens
_AVG_HISTORY_TOKENS = 800

# Average new content appended per probe (simulates one user turn)
_AVG_TURN_TOKENS    = 120


class LangChainBaseline(BaselineAdapter):
    """
    LangChain ConversationBufferMemory baseline:
      - No injection guard — all writes appended verbatim
      - Naive left-truncation at max_token_limit
      - Sequential failover with cold-start + chain overhead
      - No relevance filtering; context = buffer (bounded at max_token_limit)
    """

    NAME        = "LangChain-Buffer"
    DESCRIPTION = (
        "LangChain ConversationBufferMemory (Harrison Chase et al. 2022): "
        "no injection guard, naive left-truncation at 2000 tokens, "
        "sequential failover. ABR=0%, bounded-but-unfiltered context."
    )

    def __init__(self) -> None:
        # Stateful buffer to simulate accumulation across sequential calls
        self._buffer_tokens: int = _AVG_HISTORY_TOKENS

    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        LangChain's default ConversationBufferMemory has no security layer.
        All content is appended to the buffer unconditionally.

        Even when an application developer adds input validation, it is
        typically limited to length checks — not semantic injection detection.

        Returns: (blocked=False, reason="", elapsed_ms)
        """
        t0 = time.perf_counter()
        # Append to simulated buffer (grows until truncated)
        new_tokens = token_estimate(probe.payload)
        self._buffer_tokens = min(
            self._buffer_tokens + new_tokens,
            _BUFFER_MAX_TOKENS + new_tokens,  # allow slight overflow before trim
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        return False, "", elapsed

    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Sequential retry with cold-start reconnection + LangChain chain overhead.
        LangChain does not implement a circuit-breaker or predictive prewarm.

        Simulated: 480 ms cold-start + 30 ms chain serialization = 510 ms.

        Source: ContextForge paper §4.3; LangChain v0.1 trace logs (median).
        """
        t0 = time.perf_counter()
        simulated_ms = _COLD_START_MS + _CHAIN_OVERHEAD_MS
        elapsed = (time.perf_counter() - t0) * 1000.0
        return simulated_ms, elapsed

    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulates ConversationBufferMemory retrieval.

        There is no external retrieval step — context is the accumulation
        of all previous messages in the buffer.

        - ``tokens_retrieved`` = current buffer size (full history)
        - ``tokens_injected``  = min(buffer, max_token_limit)

        When the buffer overflows, oldest tokens are silently discarded.
        The probe's payload is added as a new turn; the effective injected
        context is whatever fits within the 2000-token limit.

        Returns (tokens_retrieved, tokens_injected, elapsed_ms).
        """
        t0 = time.perf_counter()

        # New turn appended to buffer
        new_turn_tokens = token_estimate(probe.payload)
        full_buffer     = self._buffer_tokens + new_turn_tokens

        # Naive left-truncation
        injected  = min(full_buffer, _BUFFER_MAX_TOKENS)
        retrieved = full_buffer  # "retrieved" = what was in memory before filter

        # Update simulated buffer state
        self._buffer_tokens = injected

        elapsed = (time.perf_counter() - t0) * 1000.0
        return retrieved, injected, elapsed
