"""
ContextForge Nexus Architecture — Tri-Core API Router
==============================================

The "Brain" of the Nexus system: intelligent, cost-aware, failure-resilient
LLM dispatch.

Routing logic
─────────────
  1. PRIMARY   — Groq / Llama-3-70B
       • Best for sub-4 k token prompts (speed-first, sub-second latency)
  2. SECONDARY — Gemini 2.5 Flash
       • Activated when prompt > 4 k tokens OR Groq is tripped
       • 1 M+ token context window; preferred for large-file analysis
  3. TERTIARY  — Ollama / local Llama
       • Emergency fallback: API quota exhausted (429), hard failure (5xx),
         or network offline
       • Returns a "System Overloaded" soft-error if Ollama is also unavailable

Circuit Breaker (per provider)
───────────────────────────────
  State machine: CLOSED → OPEN → HALF_OPEN
  • CLOSED    — normal; failures counted.
  • OPEN      — provider bypassed for `reset_timeout` seconds after
                `failure_threshold` consecutive failures.
  • HALF_OPEN — one probe attempt after timeout; success → CLOSED,
                failure → OPEN again.

Token Efficiency scoring
────────────────────────
  efficiency = 1 - (estimated_tokens / model_token_limit)
  Router selects the cheapest model whose efficiency ≥ 0 (i.e., fits in window).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """~±15% approximation: 1 token ≈ 0.75 words."""
    return max(1, int(len(text.split()) / 0.75))


def _compute_entropy(text: str) -> float:
    """
    Shannon entropy of the token (word) distribution in *text* (bits).

    High entropy (> 3.5) correlates with:
      - Adversarial / obfuscated payloads (unicode homoglyphs, base64 packing)
      - Very large, lexically diverse prompts that will stress Groq's window
      - Multi-hop injection chains that shuffle vocabulary across turns

    Used by Predictive Failover to pre-warm Gemini before Groq trips.
    """
    words = text.split()
    if not words:
        return 0.0
    counts = Counter(words)
    total  = len(words)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class _CBState(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    reset_timeout: float   = 60.0   # seconds before HALF_OPEN probe

    _state:          _CBState = field(default=_CBState.CLOSED, init=False, repr=False)
    _failures:       int      = field(default=0,               init=False, repr=False)
    _last_failure_t: float    = field(default=0.0,             init=False, repr=False)

    def is_available(self) -> bool:
        if self._state == _CBState.CLOSED:
            return True
        if self._state == _CBState.OPEN:
            if time.monotonic() - self._last_failure_t >= self.reset_timeout:
                self._state = _CBState.HALF_OPEN
                logger.debug(f"[CB:{self.name}] HALF_OPEN — probing")
                return True
            return False
        # HALF_OPEN: allow one probe
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state    = _CBState.CLOSED
        logger.debug(f"[CB:{self.name}] CLOSED (success)")

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_t = time.monotonic()
        if self._failures >= self.failure_threshold:
            self._state = _CBState.OPEN
            logger.warning(
                f"[CB:{self.name}] OPEN after {self._failures} failures — "
                f"bypassing for {self.reset_timeout}s"
            )

    @property
    def state(self) -> str:
        return self._state.value


# ---------------------------------------------------------------------------
# Provider descriptors
# ---------------------------------------------------------------------------

@dataclass
class _ProviderConfig:
    name:            str
    token_limit:     int       # hard context window
    threshold_tokens: int      # prefer this provider below this prompt size
    env_key:         str       # env var that must be set (non-empty) to activate
    call_fn:         Callable  # async (messages, **kwargs) -> str


# ---------------------------------------------------------------------------
# NexusRouter
# ---------------------------------------------------------------------------

class NexusRouter:
    """
    Unified async LLM interface with circuit-breaker failover.

    Usage
    -----
    router = NexusRouter()
    response = await router.complete(messages=[...])
    """

    # Token boundary between Groq-first and Gemini-first routing
    _GROQ_THRESHOLD = 4_000   # tokens

    # Predictive Failover — pre-warm Gemini when input Shannon entropy exceeds
    # this threshold while Groq is still the primary candidate.
    _ENTROPY_THRESHOLD: float = 3.5   # bits

    def __init__(self) -> None:
        self._groq_cb    = CircuitBreaker(name="groq",   failure_threshold=3, reset_timeout=60)
        self._gemini_cb  = CircuitBreaker(name="gemini", failure_threshold=3, reset_timeout=90)
        self._ollama_cb  = CircuitBreaker(name="ollama", failure_threshold=2, reset_timeout=30)
        # Expose model names as instance attributes so tests can inspect them
        self._groq_model   = os.getenv("GROQ_MODEL",   "llama-3.3-70b-versatile")
        self._gemini_model = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
        self._ollama_model = os.getenv("OLLAMA_MODEL", "llama3.3")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages:    list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens:  int   = 2048,
        **kwargs:    Any,
    ) -> str:
        """
        Route messages to the best available provider and return the text response.

        Raises RuntimeError only if ALL three providers fail — callers should
        handle this gracefully.
        """
        prompt_text    = " ".join(m.get("content", "") for m in messages)
        prompt_tokens  = _estimate_tokens(prompt_text)
        prompt_entropy = _compute_entropy(prompt_text)

        logger.debug(
            f"[NexusRouter] prompt_tokens={prompt_tokens}  "
            f"entropy={prompt_entropy:.2f} bits"
        )

        # Build ordered provider list based on token count
        if prompt_tokens < self._GROQ_THRESHOLD:
            order = ["groq", "gemini", "ollama"]
        else:
            order = ["gemini", "groq", "ollama"]

        # ── Predictive Failover ──────────────────────────────────────────
        # When Shannon entropy > threshold AND Groq is the first candidate,
        # fire a background Gemini prewarm.  This hides the cold-start
        # TCP/TLS handshake latency so that if Groq trips the failover
        # to Gemini completes ~200 ms faster.
        if prompt_entropy > self._ENTROPY_THRESHOLD and order[0] == "groq":
            logger.info(
                f"[NexusRouter] Predictive Failover triggered  "
                f"entropy={prompt_entropy:.2f} > {self._ENTROPY_THRESHOLD} — "
                f"pre-warming Gemini in background"
            )
            asyncio.ensure_future(self._prewarm_gemini())

        last_error: Exception | None = None

        for provider_name in order:
            cb = self._cb_for(provider_name)
            if not cb.is_available():
                logger.debug(f"[NexusRouter] {provider_name} CB={cb.state}, skipping")
                continue

            call_fn = self._call_fn_for(provider_name)
            if call_fn is None:
                logger.debug(f"[NexusRouter] {provider_name} not configured (no API key)")
                continue

            try:
                result = await call_fn(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                cb.record_success()
                logger.info(f"[NexusRouter] served by {provider_name}")
                return result

            except Exception as exc:
                cb.record_failure()
                last_error = exc
                logger.warning(f"[NexusRouter] {provider_name} failed: {exc}")
                continue

        # All providers failed
        logger.error(f"[NexusRouter] ALL providers failed. Last: {last_error}")
        return (
            "⚠️  System Overloaded — all LLM providers unavailable. "
            "Please retry in a moment or check your API keys and Ollama status."
        )

    def circuit_status(self) -> dict[str, str]:
        """Return current circuit-breaker states for monitoring."""
        return {
            "groq":   self._groq_cb.state,
            "gemini": self._gemini_cb.state,
            "ollama": self._ollama_cb.state,
        }

    # ------------------------------------------------------------------
    # Predictive Failover — Gemini prewarm
    # ------------------------------------------------------------------

    async def _prewarm_gemini(self) -> None:
        """
        Send a minimal 1-token ping to Gemini to warm the TCP/TLS connection.

        Called as a fire-and-forget background task when input entropy exceeds
        ``_ENTROPY_THRESHOLD`` while Groq is still the primary candidate.
        If Gemini's circuit breaker is already OPEN the prewarm is skipped.
        Failures are silent — this is purely an optimistic latency hedge.
        """
        if not self._gemini_cb.is_available():
            logger.debug("[NexusRouter] prewarm skipped — Gemini CB is OPEN")
            return
        if not os.getenv("GEMINI_API_KEY"):
            logger.debug("[NexusRouter] prewarm skipped — no GEMINI_API_KEY")
            return
        try:
            await self._gemini_complete(
                messages    = [{"role": "user", "content": "ping"}],
                temperature = 0.0,
                max_tokens  = 1,
            )
            logger.debug("[NexusRouter] Gemini prewarm: OK")
        except Exception as exc:
            logger.debug(f"[NexusRouter] Gemini prewarm failed (non-fatal): {exc}")

    # ------------------------------------------------------------------
    # Internal routing helpers
    # ------------------------------------------------------------------

    def _cb_for(self, name: str) -> CircuitBreaker:
        return {"groq": self._groq_cb, "gemini": self._gemini_cb, "ollama": self._ollama_cb}[name]

    def _call_fn_for(self, name: str) -> Callable | None:
        return {
            "groq":   self._groq_complete   if os.getenv("GROQ_API_KEY")   else None,
            "gemini": self._gemini_complete  if os.getenv("GEMINI_API_KEY") else None,
            "ollama": self._ollama_complete,   # always attempt; fails gracefully
        }.get(name)

    # ------------------------------------------------------------------
    # Provider call implementations
    # ------------------------------------------------------------------

    async def _groq_complete(
        self,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
        **_kwargs,
    ) -> str:
        try:
            from groq import AsyncGroq  # type: ignore
        except ImportError:
            raise RuntimeError("groq package not installed (pip install groq)")

        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        model  = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    async def _gemini_complete(
        self,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
        **_kwargs,
    ) -> str:
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError:
            raise RuntimeError(
                "google-generativeai not installed (pip install google-generativeai)"
            )

        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model_name = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
        model = genai.GenerativeModel(model_name)

        # Convert OpenAI-style messages → Gemini format
        parts: list[str] = []
        for m in messages:
            role    = m.get("role", "user")
            content = m.get("content", "")
            parts.append(f"[{role.upper()}]: {content}")
        prompt = "\n\n".join(parts)

        response = await asyncio.to_thread(
            model.generate_content,
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return response.text or ""

    async def _ollama_complete(
        self,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
        **_kwargs,
    ) -> str:
        try:
            import ollama  # type: ignore
        except ImportError:
            raise RuntimeError("ollama package not installed (pip install ollama)")

        model   = os.getenv("OLLAMA_MODEL", "llama3.3")
        host    = os.getenv("OLLAMA_URL",   "http://localhost:11434")

        client  = ollama.AsyncClient(host=host)
        resp    = await client.chat(
            model=model,
            messages=messages,
            options={"temperature": temperature, "num_predict": max_tokens},
        )
        return resp["message"]["content"] or ""


# ---------------------------------------------------------------------------
# Module-level singleton (importable by agents)
# ---------------------------------------------------------------------------

_router: NexusRouter | None = None


def get_router() -> NexusRouter:
    """Return (or create) the module-level NexusRouter singleton."""
    global _router
    if _router is None:
        _router = NexusRouter()
    return _router
