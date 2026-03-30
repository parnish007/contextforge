"""
ContextForge v3.0 — Token-Gater: Cost Routing

Routes LLM calls between local (Ollama) and cloud (Gemini/Groq) based on
the estimated token count of the prompt being submitted.

Routing rule (configurable via env):
  prompt token estimate < TOKEN_THRESHOLD  → local_fn  (Ollama)
  prompt token estimate >= TOKEN_THRESHOLD → cloud_fn  (Gemini / Groq)

Default threshold: 500 tokens  (env: TOKEN_ROUTER_THRESHOLD)

Usage in main.py:
    router = TokenRouter(
        local_fn=_make_model_fn_from_spec(ollama_spec),
        cloud_fn=_make_model_fn_from_spec(gemini_spec),
        threshold=500,
    )
    # Pass router.route as the model_fn to any agent
    agent = GhostCoderAgent(..., model_spec=None)
    agent._distiller = SemanticDistiller(model_fn=router.route, ...)
"""

from __future__ import annotations

import os
from typing import Callable

from loguru import logger

ModelFn = Callable[[list[dict]], str]

_DEFAULT_THRESHOLD = int(os.getenv("TOKEN_ROUTER_THRESHOLD", "500"))


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: sum of all message content lengths / 4."""
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, total_chars // 4)


class TokenRouter:
    """
    Routes model_fn calls by prompt token count.

    Parameters
    ----------
    local_fn : ModelFn | None
        Callable for short prompts (< threshold). Typically Ollama.
        Falls through to cloud_fn if None.
    cloud_fn : ModelFn | None
        Callable for long prompts (>= threshold). Typically Gemini/Groq.
        Falls through to local_fn if None.
    threshold : int
        Token boundary between local and cloud.  Default 500.
    """

    def __init__(
        self,
        local_fn: ModelFn | None = None,
        cloud_fn: ModelFn | None = None,
        threshold: int = _DEFAULT_THRESHOLD,
    ):
        self._local = local_fn
        self._cloud = cloud_fn
        self._threshold = threshold

        if local_fn is None and cloud_fn is None:
            logger.warning("TokenRouter: both local_fn and cloud_fn are None — all calls will fail")

        local_label = "Ollama" if local_fn else "none"
        cloud_label = "Gemini/Groq" if cloud_fn else "none"
        logger.info(
            f"TokenRouter: threshold={threshold} tokens | "
            f"<{threshold} → {local_label} | "
            f">={threshold} → {cloud_label}"
        )

    @property
    def has_any(self) -> bool:
        return self._local is not None or self._cloud is not None

    def route(self, messages: list[dict]) -> str:
        """
        Choose local or cloud fn by token count, call it, return the response.
        Raises RuntimeError if no backend is available.
        """
        tokens = _estimate_tokens(messages)
        use_local = tokens < self._threshold

        if use_local and self._local is not None:
            logger.debug(f"TokenRouter: ~{tokens} tokens → LOCAL (Ollama)")
            return self._local(messages)

        if not use_local and self._cloud is not None:
            logger.debug(f"TokenRouter: ~{tokens} tokens → CLOUD (Gemini/Groq)")
            return self._cloud(messages)

        # Fallback: try whatever is available
        if self._cloud is not None:
            logger.debug(f"TokenRouter: local unavailable → falling back to CLOUD")
            return self._cloud(messages)

        if self._local is not None:
            logger.debug(f"TokenRouter: cloud unavailable → falling back to LOCAL")
            return self._local(messages)

        raise RuntimeError("TokenRouter: no model backend available")

    def describe(self) -> str:
        parts = []
        if self._local:
            parts.append(f"local(<{self._threshold}tok)")
        if self._cloud:
            parts.append(f"cloud(>={self._threshold}tok)")
        return " | ".join(parts) if parts else "no-backend"
