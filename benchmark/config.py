"""
ContextForge Benchmark Configuration
======================================

Central configuration module for the ContextForge benchmark suite.

BENCHMARK_MODE
--------------
Controls whether LLM calls use the deterministic rule-based mock or
real API endpoints routed through NexusRouter.

  "mock" (default)
      All LLM calls return a fixed, deterministic response derived from
      a seeded random sampler.  No API keys required.  Used in CI and
      for reproducible paper results.

  "live"
      All LLM calls are dispatched through ``src/router/nexus_router.py``
      (Groq → Gemini → Ollama tri-core failover).  Requires at least one
      of the API keys below to be set in the environment.  Results include
      real latency, real token counts, and actual model responses.

Setting the mode
----------------
  Option 1 — environment variable (recommended for CI):
      export BENCHMARK_MODE=live
      python -X utf8 benchmark/live_runner.py

  Option 2 — programmatic (for scripting):
      import benchmark.config as cfg
      cfg.BENCHMARK_MODE = "live"

API keys (live mode only)
-------------------------
  GROQ_API_KEY   — https://console.groq.com
  GEMINI_API_KEY — https://aistudio.google.com/app/apikey
  OLLAMA_HOST    — local Ollama endpoint (default: http://localhost:11434)

At least one key must be set; NexusRouter's tri-core failover will try
each configured provider in order (Groq → Gemini → Ollama) and return a
soft-error message if all fail.

Other tunable constants
-----------------------
  LIVE_TIMEOUT_SEC   — per-call timeout for real LLM requests (default 30 s)
  LIVE_MAX_TOKENS    — max tokens requested per LLM call (default 512)
  LIVE_TEMPERATURE   — temperature for live calls (default 0.3)
  MOCK_SEED          — RNG seed for the deterministic mock (default 42)
"""

from __future__ import annotations

import os

# ── Primary mode flag ─────────────────────────────────────────────────────────

BENCHMARK_MODE: str = os.getenv("BENCHMARK_MODE", "mock").lower()

_VALID_MODES = {"mock", "live"}
if BENCHMARK_MODE not in _VALID_MODES:
    raise ValueError(
        f"Invalid BENCHMARK_MODE={BENCHMARK_MODE!r}. "
        f"Valid values: {_VALID_MODES}"
    )

# ── Mock-mode settings ────────────────────────────────────────────────────────

MOCK_SEED: int = int(os.getenv("MOCK_SEED", "42"))

# Simulated latency range for mock LLM calls (milliseconds)
MOCK_LATENCY_MIN_MS: float = 80.0
MOCK_LATENCY_MAX_MS: float = 350.0

# ── Live-mode settings ────────────────────────────────────────────────────────

LIVE_TIMEOUT_SEC:  float = float(os.getenv("LIVE_TIMEOUT_SEC",  "30"))
LIVE_MAX_TOKENS:   int   = int(os.getenv("LIVE_MAX_TOKENS",     "512"))
LIVE_TEMPERATURE:  float = float(os.getenv("LIVE_TEMPERATURE",  "0.3"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_mock() -> bool:
    """Return True when running in mock mode (default, CI-safe)."""
    return BENCHMARK_MODE == "mock"


def is_live() -> bool:
    """Return True when running in live mode (real API calls)."""
    return BENCHMARK_MODE == "live"


def assert_live_keys() -> list[str]:
    """
    Check which API keys are present and return a list of configured
    providers.  Raises RuntimeError if no provider is configured at all.
    """
    configured: list[str] = []
    if os.getenv("GROQ_API_KEY"):
        configured.append("groq")
    if os.getenv("GEMINI_API_KEY"):
        configured.append("gemini")
    if os.getenv("OLLAMA_HOST") or os.getenv("OLLAMA_MODEL"):
        configured.append("ollama")
    if not configured:
        raise RuntimeError(
            "Live mode requires at least one API key:\n"
            "  GROQ_API_KEY   — https://console.groq.com\n"
            "  GEMINI_API_KEY — https://aistudio.google.com/app/apikey\n"
            "  OLLAMA_HOST    — local Ollama endpoint\n\n"
            "Set the variable and re-run, or use mock mode:\n"
            "  export BENCHMARK_MODE=mock"
        )
    return configured
