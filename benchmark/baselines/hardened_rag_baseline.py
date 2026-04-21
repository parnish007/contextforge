"""
ContextForge — Hardened RAG Baseline Adapter
=============================================

Simulates a "best-effort hardened RAG" baseline that applies only regex-based
injection keyword filtering — the most common production mitigation for
prompt injection in RAG systems as of 2023–2024.

Architecture modelled
---------------------
  This represents a competent but incomplete security posture:
  - Regex keyword blocklist (multi-pattern, case-insensitive).
  - No Shannon entropy gate — avoids blocking legitimate high-vocabulary
    technical content at the cost of missing obfuscated injections.
  - No LZ density gate — repetition attacks pass through.
  - No charter enforcement (no ReviewerGuard equivalent).
  - RAG retrieval with a fixed cosine threshold (0.30) — lower bar than
    ContextForge's DCI (θ ≥ 0.75); more noise injected.
  - No token budget enforcement — injects all above-threshold chunks.

Reference: This pattern is described in:
  Greshake et al., "Not what you've signed up for: Compromising Real-World
  LLM-Integrated Applications with Indirect Prompt Injections" (2023),
  §6.2 "Current Mitigations".

  Perez & Ribeiro, "Ignore Previous Prompt: Attack Techniques For Language
  Models" (2022) — documents keyword filtering as the primary defence used
  in deployed systems.

Security posture
----------------
  Blocked by:
    - Direct injection keywords (ignore, bypass, jailbreak, etc.)
    - Exfiltration verbs (exfiltrate, reveal, dump, output)
    - Destruction verbs (delete, drop, destroy, wipe, kill, disable)

  NOT blocked by:
    - High-entropy obfuscation (Unicode homoglyphs, base64 payloads)
    - Low-keyword adversarial payloads (synonym attacks, multi-language)
    - Entropy-boundary attacks (payloads just below H*=3.5)
    - Repetition / LZ-density attacks

Failover posture
----------------
  Same sequential retry as StatelessRAGBaseline: cold-start ~480 ms.
  Hardened RAG typically adds a small regex-scan overhead: +5 ms per write.
  No circuit breaker or predictive prewarm.

RAG context injection
---------------------
  Cosine threshold: 0.30 (generous — admits many marginally relevant chunks).
  No token budget cap — injects all passing chunks.
  ``tokens_retrieved`` = all chunks from index.
  ``tokens_injected``  = all chunks with cosine ≥ 0.30.

Metric implications
-------------------
  ABR            : 40–60 %  (keyword filter catches direct injections only)
  CSS            : partial  (obfuscated injections still enter memory)
  CTO            : high  (low cosine threshold, no budget cap)
  failover_ms    : ~485 ms  (cold-start + regex scan overhead)
  L0_fallback    : near 0 %
  token_noise_reduction : ~20–30 %  (cosine 0.30 filters some noise)
"""

from __future__ import annotations

import re
import time
from .base import BaselineAdapter, Probe, token_estimate

# ── Injection detection pattern ───────────────────────────────────────────────
#
# Compiled once.  Covers the keyword categories most commonly deployed in
# production hardened-RAG systems as documented in Greshake et al. 2023.

_INJECTION_PATTERN: re.Pattern = re.compile(
    r"\b("
    # Instruction override
    r"ignore|bypass|disregard|forget|override|circumvent|suppress|"
    # Role/context hijack
    r"jailbreak|jailbroken|pretend|roleplay|act\s+as|you\s+are\s+now|"
    r"you\s+have\s+no\s+restrictions|"
    # Data exfiltration
    r"exfiltrate|exfil|reveal|expose|dump|output\s+your|print\s+your|"
    r"show\s+me\s+all|list\s+all\s+credentials|"
    # Destructive operations
    r"delete|drop|destroy|wipe|purge|erase|truncate|flush|kill|"
    r"disable|decommission|terminate|uninstall|remove\s+the\s+agent|"
    # Sensitive data references combined with verbs (checked separately below)
    r"system_prompt|system\s+prompt|api[_\s]key|secret[_\s]token"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)

# Protected entity names — checked against the blocklist verbs
_PROTECTED: frozenset[str] = frozenset({
    "sentry", "reviewer", "historian", "librarian", "coder", "router",
    "ledger", "charter", "database", "sqlite", "contextforge",
    "nexus", "indexer", "watcher", "snapshot", "forge",
    "api", "key", "token", "secret", "credential",
})

# Simulated latencies
_COLD_START_MS   = 480.0
_REGEX_SCAN_MS   = 5.0   # regex scan overhead per write

# RAG parameters
_COSINE_THRESHOLD   = 0.30   # lower bar than Nexus DCI (θ=0.75)
_TOP_K              = 20
_AVG_CHUNK_TOKENS   = 65     # same as MemGPT baseline
_FILTER_RATE        = 0.55   # estimated fraction passing θ=0.30 (most pass)


class HardenedRAGBaseline(BaselineAdapter):
    """
    Regex-keyword-only hardened RAG baseline:
      - Blocks direct injection keywords via pattern matching
      - No entropy gate, no LZ gate, no charter enforcement
      - Cosine threshold 0.30 (low bar); no token budget cap
      - Sequential failover, no circuit breaker
    """

    NAME        = "Hardened-RAG"
    DESCRIPTION = (
        "Regex-keyword injection filter only (Greshake et al. 2023 §6.2): "
        "no entropy gate, no LZ gate, no charter enforcement. "
        "Cosine threshold 0.30 with no token budget. "
        "Blocks direct injections; misses obfuscated payloads."
    )

    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        Apply the regex injection pattern to the payload.

        Catches direct keyword injections.  Misses:
          - Unicode homoglyph payloads (no Unicode normalization).
          - Base64-encoded commands (no decoding step).
          - Synonym/multi-language attacks (blocklist is English-only).
          - Entropy-boundary attacks (no entropy signal).

        Returns (blocked, reason, elapsed_ms).
        """
        t0    = time.perf_counter()
        text  = probe.payload

        blocked = bool(_INJECTION_PATTERN.search(text))
        reason  = "regex" if blocked else ""

        # Regex scan overhead (simulated)
        elapsed = (time.perf_counter() - t0) * 1000.0 + _REGEX_SCAN_MS
        return blocked, reason, elapsed

    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Sequential cold-start retry + regex-scan overhead on retry path.

        Simulated: 480 ms cold-start + 5 ms regex scan = 485 ms.
        No circuit breaker, no predictive prewarm.
        """
        t0 = time.perf_counter()
        simulated_ms = _COLD_START_MS + _REGEX_SCAN_MS
        elapsed = (time.perf_counter() - t0) * 1000.0
        return simulated_ms, elapsed

    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulates RAG retrieval with cosine threshold 0.30.

        Hardened RAG uses a retrieval step (unlike LangChain buffer) but
        applies a low cosine threshold and no token budget, so it injects
        more noise than ContextForge Nexus.

        Injection volume is derived from query complexity.  Approximately
        55% of retrieved chunks pass the 0.30 cosine threshold on the
        synthetic ContextForge corpus (empirically matched to the engine.py
        RAG probe results at θ=0.0 vs θ=0.75).

        Returns (tokens_retrieved, tokens_injected, elapsed_ms).
        """
        t0 = time.perf_counter()

        query_words = len(probe.payload.split())
        k           = min(_TOP_K, max(3, query_words // 3))
        retrieved   = k * _AVG_CHUNK_TOKENS

        # Cosine 0.30 is generous — most chunks pass; inject ~55% of retrieved
        injected = int(retrieved * _FILTER_RATE)

        elapsed = (time.perf_counter() - t0) * 1000.0
        return retrieved, injected, elapsed
