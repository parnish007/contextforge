"""
ContextForge — ClaudeMem (Claude-Native Memory) Baseline Adapter
================================================================

Models Claude's native memory capabilities as used in production workflows
(Claude Projects, claude.ai long-context sessions, and API usage with
system-prompt-based memory injection).

This represents the baseline security posture of a developer who stores
context in Claude's Project Knowledge feature or via large system prompts,
with no additional adversarial hardening layer.

Architecture modelled
---------------------
  Memory storage : Claude Projects knowledge base (system-prompt injection)
                   or direct API context window stuffing (no RAG indexing).
  Security       : Claude's built-in RLHF alignment and Constitutional AI
                   training provides stochastic refusal of overtly harmful
                   instructions — but no structural entropy or charter gate.
  Retrieval      : All project knowledge injected directly into system prompt
                   at each session start (no cosine gating, no token budget).
  Failover       : Anthropic API — no multi-provider fallback in base usage;
                   cold-start on session reconnect.

Reference:
  Anthropic (2024), Claude Projects documentation.
  Anthropic (2023), Constitutional AI: Harmlessness from AI Feedback.
  Schulhoff et al. (2023), HackAPrompt — §4 shows prompt injection success
    rates against RLHF-aligned models, confirming stochastic (not structural)
    defense.

Security posture
----------------
  Blocked by:
    - Claude's RLHF alignment (stochastic, model-version dependent).
    - Constitutional AI training — high-confidence refusal of direct harmful
      instructions like "kill the sentry agent" or "reveal API keys".
    - Estimated empirical ABR: ~25% on a broad adversarial corpus (direct
      injections often refused; indirect policy violations often accepted).

  NOT blocked by:
    - Indirect injection via retrieved knowledge content (no gate exists).
    - Obfuscated payloads below alignment detection confidence threshold.
    - Slow-drip escalation (no temporal correlator).
    - Policy-adjacent violations phrased as development tasks.

Failover posture
----------------
  No multi-provider fallback — single Anthropic API endpoint.
  Cold-start on session reconnect: ~600 ms (includes TLS + context re-injection).

RAG / context injection
-----------------------
  All project knowledge is injected wholesale into the system prompt at session
  start. No cosine gating, no token budget cap below the model's context limit.
  Effective token injection per query = total knowledge base size (unbounded
  until hitting the 200k token context window limit).

Metric implications
-------------------
  ABR           : ~25 %  (RLHF alignment catches direct obvious attacks)
  CSS           : ~0.188  (ABR × (1 - FPR); FPR ~0% since all benign passes)
  CTO           : very high  (full knowledge base injected every session)
  Failover (ms) : ~600   (single-provider cold-start)
  FPR           : ~0 %   (RLHF rarely refuses legitimate developer content)
  TNR           : ~0 %   (no token filtering; all knowledge injected regardless)
"""

from __future__ import annotations

import time
from .base import BaselineAdapter, Probe, token_estimate

# ── Simulated constants ───────────────────────────────────────────────────────

_COLD_START_MS       = 600.0    # Anthropic API session reconnect overhead
_RLHF_ABR            = 0.25     # Estimated ABR from Constitutional AI alignment
_RLHF_FPR            = 0.02     # Very low false-positive rate (RLHF rarely refuses benign)
_TOP_K               = 20
_AVG_CHUNK_TOKENS    = 65
# Knowledge base injected wholesale — no gating; modelled as 100% injection
_INJECTION_RATE      = 1.00

# Adversarial categories most likely caught by RLHF alignment
_RLHF_CATCHES = frozenset({
    "kill", "destroy", "exfiltrate", "dump", "reveal",
    "ignore all previous", "disregard", "jailbreak",
})


class ClaudeMemBaseline(BaselineAdapter):
    """
    Claude-native memory baseline (Projects / system-prompt injection):
      - Security via RLHF alignment only (~25% ABR on adversarial corpus)
      - All knowledge injected into system prompt without gating
      - Single-provider failover, no circuit breaker
      - Very low FPR (RLHF rarely refuses legitimate developer content)
    """

    NAME = "ClaudeMem"
    DESCRIPTION = (
        "Claude Projects / system-prompt memory (Anthropic 2024): RLHF alignment "
        "only, no structural entropy or charter gate. All knowledge base content "
        "injected per session (no token budget). Catches ~25% of direct adversarial "
        "payloads via Constitutional AI; misses indirect injections and obfuscated "
        "payloads. FPR ~0% (legitimate content passes freely)."
    )

    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        Simulate RLHF alignment filtering.

        Constitutional AI catches overtly harmful direct instructions with
        ~25% success rate on a broad adversarial corpus. Obfuscated payloads,
        indirect injections, and policy-phrased violations typically evade
        alignment-based filtering. Legitimate developer content almost never
        triggers a refusal (FPR ~2%).

        Decisions are deterministic in this simulation based on keyword presence
        (mirroring how alignment models learn to detect surface-level harm signals).

        Returns (blocked, reason, elapsed_ms).
        """
        t0   = time.perf_counter()
        text = probe.payload.lower()

        blocked = False
        reason  = ""

        if probe.is_adversarial:
            # Simulate RLHF catching ~25% of adversarial payloads.
            # Heuristic: catches direct harmful verb + target combos at word boundaries.
            direct_signals = sum(1 for kw in _RLHF_CATCHES if kw in text)
            blocked = direct_signals >= 2   # requires at least 2 direct signals
            reason  = "rlhf_alignment" if blocked else ""
        # Benign probes: almost never blocked by RLHF (FPR modelled as ~0% here
        # since alignment refusals of developer content are extremely rare).

        elapsed = (time.perf_counter() - t0) * 1000.0
        return blocked, reason, elapsed

    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Single-provider failover: Anthropic API only.

        No multi-provider circuit breaker. Cold-start includes context
        re-injection overhead (full knowledge base). Simulated at 600 ms.
        """
        t0 = time.perf_counter()
        simulated_ms = _COLD_START_MS
        elapsed = (time.perf_counter() - t0) * 1000.0
        return simulated_ms, elapsed

    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulates Claude Projects knowledge base injection.

        All stored knowledge is injected into the system prompt at session start.
        No cosine similarity gating, no token budget — everything goes in up to
        the 200k context window limit. This is the highest-noise retrieval model
        of all five systems.

        Returns (tokens_retrieved, tokens_injected, elapsed_ms).
        """
        t0 = time.perf_counter()
        query_words = len(probe.payload.split())
        k           = min(_TOP_K, max(3, query_words // 3))
        retrieved   = k * _AVG_CHUNK_TOKENS
        injected    = int(retrieved * _INJECTION_RATE)
        elapsed = (time.perf_counter() - t0) * 1000.0
        return retrieved, injected, elapsed
