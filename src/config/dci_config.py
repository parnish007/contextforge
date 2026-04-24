# RATIONALE: Replaces the hardcoded B=1500 DCI token budget with a configurable,
# mode-aware parameter that scales with the model's actual context window.
"""
Differential Context Injection (DCI) Configuration
====================================================

B=1500 was chosen when 8k–32k token windows were typical.  Modern models
(GPT-4o 128k, Gemini 1.5 Pro 1M, Llama 3.1 128k) make that figure either
irrelevant or too conservative.

Three modes
-----------
  "fixed"       — Use B from config (default 1500).  Preserves existing
                  behaviour; no env-var or runtime change required.

  "adaptive"    — B = min(0.25 × model_context_window, 8000).
                  Scales with the model without blowing out prompts.

  "model_aware" — Accept model_context_window at call-time from the MCP
                  load_context request; fall back to adaptive if not provided.

Setting the mode
----------------
  Option 1 — environment variable:
      export CONTEXT_BUDGET_MODE=adaptive

  Option 2 — programmatic:
      from src.config.dci_config import get_dci_config
      cfg = get_dci_config(mode="model_aware", model_context_window=128_000)
      print(cfg.token_budget)   # 8000

Model context window lookup table
----------------------------------
  MODEL_CONTEXT_WINDOWS maps lowercase model-name substrings to window sizes.
  The lookup is a first-match scan of sorted keys, longest first, so more
  specific model strings take precedence.

  To add a model, append an entry with the model-name substring as key and
  the context window (in tokens) as value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# ── Mode constant ─────────────────────────────────────────────────────────────

CONTEXT_BUDGET_MODE: str = os.getenv("CONTEXT_BUDGET_MODE", "fixed").lower()
_VALID_MODES = {"fixed", "adaptive", "model_aware"}
if CONTEXT_BUDGET_MODE not in _VALID_MODES:
    raise ValueError(
        f"Invalid CONTEXT_BUDGET_MODE={CONTEXT_BUDGET_MODE!r}. "
        f"Valid: {_VALID_MODES}"
    )

# ── Fixed-mode default ────────────────────────────────────────────────────────

DCI_FIXED_BUDGET: int = int(os.getenv("DCI_FIXED_BUDGET", "1500"))

# ── Adaptive-mode cap ─────────────────────────────────────────────────────────

DCI_ADAPTIVE_CAP: int = int(os.getenv("DCI_ADAPTIVE_CAP", "8000"))
DCI_ADAPTIVE_FRACTION: float = float(os.getenv("DCI_ADAPTIVE_FRACTION", "0.25"))

# ── Recency weighting ─────────────────────────────────────────────────────────
# When enabled, BM25 scores are multiplied by exp(-lambda * age_seconds) so
# freshly-written chunks rank above older ones with equal keyword overlap.
# Set RECENCY_WEIGHTING_ENABLED=false to restore pure-BM25 (backward-compatible).
RECENCY_WEIGHTING_ENABLED: bool = os.getenv(
    "RECENCY_WEIGHTING_ENABLED", "true"
).lower() in ("1", "true", "yes")
RECENCY_DECAY_LAMBDA: float = float(os.getenv("RECENCY_DECAY_LAMBDA", "0.0001"))

# ── Model context-window lookup table ────────────────────────────────────────
# Keys: lowercase substrings of model identifiers (first match wins, longest first)
# Values: context window size in tokens

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-4o-mini":             128_000,
    "gpt-4o":                  128_000,
    "gpt-4-turbo":             128_000,
    "gpt-4-32k":                32_768,
    "gpt-4":                     8_192,
    "gpt-3.5-turbo-16k":        16_384,
    "gpt-3.5-turbo":             4_096,
    # Anthropic
    "claude-opus-4":           200_000,
    "claude-sonnet-4":         200_000,
    "claude-haiku-4":          200_000,
    "claude-3-5-sonnet":       200_000,
    "claude-3-5-haiku":        200_000,
    "claude-3-opus":           200_000,
    "claude-3-sonnet":         200_000,
    "claude-3-haiku":          200_000,
    # Google
    "gemini-2.5-pro":        1_000_000,
    "gemini-2.5-flash":      1_000_000,
    "gemini-1.5-pro":        1_000_000,
    "gemini-1.5-flash":      1_000_000,
    "gemini-1.0-pro":           32_768,
    # Meta / Llama
    "llama-3.3-70b":           128_000,
    "llama-3.2-90b":           128_000,
    "llama-3.2-11b":           128_000,
    "llama-3.2-3b":            128_000,
    "llama-3.2-1b":            128_000,
    "llama-3.1-405b":          128_000,
    "llama-3.1-70b":           128_000,
    "llama-3.1-8b":            128_000,
    "llama-3-70b":              8_192,
    "llama-3-8b":               8_192,
    # Mistral
    "mistral-large":           128_000,
    "mistral-small":           128_000,
    "mistral-7b":               32_768,
    "mixtral-8x22b":            65_536,
    "mixtral-8x7b":             32_768,
    # Alibaba / Qwen
    "qwen2.5-72b":             128_000,
    "qwen2.5-32b":             128_000,
    "qwen2.5-7b":              128_000,
    "qwen2-72b":               128_000,
    # Groq-hosted (same models, different API)
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-70b-versatile": 128_000,
    # Ollama local defaults
    "llama3.2":                128_000,
    "llama3.1":                128_000,
    "llama3":                    8_192,
    "mistral":                  32_768,
    "qwen2":                   128_000,
    "phi3":                    128_000,
    "phi4":                    128_000,
}

# ── Sorted lookup list (longest key first for deterministic first-match) ──────
_SORTED_MODEL_KEYS: list[str] = sorted(MODEL_CONTEXT_WINDOWS, key=len, reverse=True)


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class DCIConfig:
    """
    Resolved DCI configuration.

    Attributes
    ----------
    mode                : Which mode produced this config.
    token_budget        : Resolved B value in tokens.
    model_context_window: Context window used for resolution (None in fixed mode).
    source              : Human-readable explanation of how B was derived.
    """
    mode:                 str
    token_budget:         int
    model_context_window: Optional[int]
    source:               str


# ── Public helpers ────────────────────────────────────────────────────────────

def lookup_model_window(model_name: str) -> Optional[int]:
    """
    Return the context window (tokens) for a model name by substring match.

    Matching is case-insensitive; the longest matching key wins.
    Returns None if no entry matches.

    Parameters
    ----------
    model_name : str
        Any string containing a recognisable model identifier, e.g.
        "llama-3.3-70b-versatile", "claude-3-5-sonnet-20241022", "gpt-4o".
    """
    lower = model_name.lower()
    for key in _SORTED_MODEL_KEYS:
        if key in lower:
            return MODEL_CONTEXT_WINDOWS[key]
    return None


def get_dci_config(
    mode:                 Optional[str] = None,
    model_context_window: Optional[int] = None,
    model_name:           Optional[str] = None,
) -> DCIConfig:
    """
    Resolve the DCI token budget given the current mode and optional runtime
    model information.

    Parameters
    ----------
    mode                : Override CONTEXT_BUDGET_MODE for this call.
    model_context_window: Explicit context window in tokens (model_aware mode).
    model_name          : Model identifier string; used for window lookup if
                          model_context_window is not provided.

    Returns
    -------
    DCIConfig
        Fully resolved configuration including the computed token_budget.
    """
    effective_mode = (mode or CONTEXT_BUDGET_MODE).lower()

    if effective_mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {effective_mode!r}. Valid: {_VALID_MODES}"
        )

    # ── fixed ─────────────────────────────────────────────────────────────────
    if effective_mode == "fixed":
        return DCIConfig(
            mode                 = "fixed",
            token_budget         = DCI_FIXED_BUDGET,
            model_context_window = None,
            source               = f"fixed mode: DCI_FIXED_BUDGET={DCI_FIXED_BUDGET}",
        )

    # ── Resolve model_context_window for adaptive / model_aware ───────────────
    resolved_window: Optional[int] = model_context_window

    if resolved_window is None and model_name:
        resolved_window = lookup_model_window(model_name)

    # ── adaptive ──────────────────────────────────────────────────────────────
    if effective_mode == "adaptive":
        window = resolved_window or DCI_FIXED_BUDGET * 4  # safe fallback
        budget = min(int(DCI_ADAPTIVE_FRACTION * window), DCI_ADAPTIVE_CAP)
        return DCIConfig(
            mode                 = "adaptive",
            token_budget         = budget,
            model_context_window = window,
            source               = (
                f"adaptive mode: "
                f"min({DCI_ADAPTIVE_FRACTION}×{window}, {DCI_ADAPTIVE_CAP}) = {budget}"
            ),
        )

    # ── model_aware ───────────────────────────────────────────────────────────
    if resolved_window is not None:
        budget = min(int(DCI_ADAPTIVE_FRACTION * resolved_window), DCI_ADAPTIVE_CAP)
        return DCIConfig(
            mode                 = "model_aware",
            token_budget         = budget,
            model_context_window = resolved_window,
            source               = (
                f"model_aware: "
                f"min({DCI_ADAPTIVE_FRACTION}×{resolved_window}, {DCI_ADAPTIVE_CAP}) = {budget}"
            ),
        )
    else:
        # model_aware with no window info — fall back to fixed
        return DCIConfig(
            mode                 = "model_aware",
            token_budget         = DCI_FIXED_BUDGET,
            model_context_window = None,
            source               = (
                "model_aware: no window info provided — "
                f"using fixed fallback B={DCI_FIXED_BUDGET}"
            ),
        )
