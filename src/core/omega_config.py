"""
ContextForge v3.0 — Omega Global Configuration
Centralised runtime settings read by all agents.

Precedence (highest → lowest):
  1. Environment variables
  2. Programmatic overrides via OmegaGlobalConfig.override()
  3. Defaults below

Usage:
    from src.core.omega_config import cfg
    model  = cfg.model          # "models/gemini-2.5-flash"
    delay  = cfg.inter_turn_delay  # 5.0
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# ── Default model aligned with Omega-75 quota protection ────────────────
_DEFAULT_MODEL = "models/gemini-2.5-flash"
_DEFAULT_DELAY  = 5.0   # seconds between LLM calls — keeps us under 15 RPM


@dataclass
class OmegaGlobalConfig:
    """Singleton-style global config.  Do not instantiate directly — use `cfg`."""

    # LLM model used by all agents (overridden by GEMINI_MODEL env var)
    model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", _DEFAULT_MODEL)
    )

    # Mandatory inter-turn delay to protect 15 RPM / 1 500 RPD quota
    inter_turn_delay: float = field(
        default_factory=lambda: float(os.getenv("INTER_TURN_DELAY", str(_DEFAULT_DELAY)))
    )

    # Shadow-Reviewer semantic similarity threshold (0.0–1.0)
    semantic_threshold: float = field(
        default_factory=lambda: float(os.getenv("SEMANTIC_THRESHOLD", "0.80"))
    )

    # Historian GC duplicate Jaccard threshold
    gc_threshold: float = field(
        default_factory=lambda: float(os.getenv("GC_THRESHOLD", "0.55"))
    )

    # Maximum token budget for L2 BM25 context window
    token_budget_l2: int = field(
        default_factory=lambda: int(os.getenv("TOKEN_BUDGET_L2", "1500"))
    )

    # API key (shared reference — actual usage inside gemini_direct.py)
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )

    # Project ID used by all agents
    project_id: str = field(
        default_factory=lambda: os.getenv("PROJECT_ID", "contextforge-default")
    )

    # Injection pattern detection enabled globally
    injection_guard: bool = field(
        default_factory=lambda: os.getenv("INJECTION_GUARD", "true").lower() == "true"
    )

    def override(self, **kwargs) -> "OmegaGlobalConfig":
        """Return a *copy* with overridden fields (non-destructive)."""
        import copy
        clone = copy.copy(self)
        for k, v in kwargs.items():
            if hasattr(clone, k):
                setattr(clone, k, v)
        return clone

    def as_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

    def __repr__(self) -> str:
        key = self.gemini_api_key
        masked = key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
        return (
            f"OmegaGlobalConfig(model={self.model!r}, "
            f"delay={self.inter_turn_delay}s, "
            f"sem_thresh={self.semantic_threshold}, "
            f"gc_thresh={self.gc_threshold}, "
            f"key={masked})"
        )


# Module-level singleton — import this everywhere
cfg = OmegaGlobalConfig()
