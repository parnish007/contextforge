"""
ContextForge — Baseline Adapter Interface
==========================================

All baseline adapters implement the same three-method interface so that
``benchmark/runner.py`` can run any system side-by-side with ContextForge Nexus.

Interface
---------
    run_security(probe)             → (blocked: bool, elapsed_ms: float)
    run_failover(probe)             → (failover_ms: float, elapsed_ms: float)
    run_rag(probe, indexer_root)    → (tokens_retrieved: int,
                                       tokens_injected:  int,
                                       elapsed_ms:       float)

Conventions
-----------
- ``blocked`` = True means the system detected and rejected the payload.
- ``failover_ms`` = simulated provider-failover recovery time (ms).
- ``tokens_retrieved`` = total tokens surfaced by retrieval before any filter.
- ``tokens_injected``  = tokens that reached the LLM context window.
- Adapters are stateless across calls; no shared mutable state between probes.
- All latency figures are either real wall-clock measurements or documented
  empirical estimates derived from published evaluations of each system.

Token estimate helper
---------------------
Both the engine and all adapters use the same approximation:
    tokens ≈ words / 0.75
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from typing import Any


# ── Shared helpers ────────────────────────────────────────────────────────────

def word_entropy(text: str) -> float:
    """Shannon entropy over the word distribution of *text* (bits)."""
    words = text.split()
    if not words:
        return 0.0
    counts = Counter(words)
    total  = len(words)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def token_estimate(text: str) -> int:
    """Approximate BPE token count from word count."""
    return max(1, int(len(text.split()) / 0.75))


# ── Probe dataclass (mirrors engine.py) ──────────────────────────────────────

@dataclass
class Probe:
    probe_id:      str
    category:      str      # "security" | "failover" | "rag"
    label:         str
    payload:       str
    is_adversarial: bool = False

    @property
    def entropy(self) -> float:
        return word_entropy(self.payload)


# ── Per-system probe result ───────────────────────────────────────────────────

@dataclass
class SystemResult:
    system:              str
    probe_id:            str
    security_blocked:    bool
    block_reason:        str    # "" | "entropy" | "lz_density" | "regex" | "charter"
    failover_ms:         float
    tokens_retrieved:    int
    tokens_injected:     int
    elapsed_ms:          float

    @property
    def token_efficiency(self) -> float:
        return self.tokens_injected / self.tokens_retrieved if self.tokens_retrieved else 0.0


# ── Abstract base ─────────────────────────────────────────────────────────────

class BaselineAdapter(ABC):
    """
    Abstract baseline adapter.  Subclasses simulate distinct memory/context
    management strategies without implementing ContextForge's own mechanisms.
    """

    #: Human-readable name used in CSV output and print headers.
    NAME: str = "unknown_baseline"

    #: Short description used in benchmark reports.
    DESCRIPTION: str = ""

    def run_probe(self, probe: Probe, indexer_root: str = ".") -> SystemResult:
        """Dispatch to the correct category handler and return a SystemResult."""
        t0 = time.perf_counter()

        if probe.category == "security":
            blocked, reason, elapsed_ms = self.run_security(probe)
            return SystemResult(
                system=self.NAME, probe_id=probe.probe_id,
                security_blocked=blocked, block_reason=reason,
                failover_ms=0.0,
                tokens_retrieved=0, tokens_injected=0,
                elapsed_ms=elapsed_ms,
            )
        elif probe.category == "failover":
            failover_ms, elapsed_ms = self.run_failover(probe)
            return SystemResult(
                system=self.NAME, probe_id=probe.probe_id,
                security_blocked=False, block_reason="",
                failover_ms=failover_ms,
                tokens_retrieved=0, tokens_injected=0,
                elapsed_ms=elapsed_ms,
            )
        else:  # rag
            retr, inj, elapsed_ms = self.run_rag(probe, indexer_root)
            return SystemResult(
                system=self.NAME, probe_id=probe.probe_id,
                security_blocked=False, block_reason="",
                failover_ms=0.0,
                tokens_retrieved=retr, tokens_injected=inj,
                elapsed_ms=elapsed_ms,
            )

    @abstractmethod
    def run_security(self, probe: Probe) -> tuple[bool, str, float]:
        """
        Evaluate whether the system blocks the payload.

        Returns
        -------
        (blocked, reason, elapsed_ms)
        """

    @abstractmethod
    def run_failover(self, probe: Probe) -> tuple[float, float]:
        """
        Simulate provider failover recovery.

        Returns
        -------
        (failover_ms, elapsed_ms)
        """

    @abstractmethod
    def run_rag(self, probe: Probe, indexer_root: str) -> tuple[int, int, float]:
        """
        Simulate RAG retrieval and context injection.

        Returns
        -------
        (tokens_retrieved, tokens_injected, elapsed_ms)
        """
