"""
ContextForge v3.0 — Direct Gemini Adapter
==========================================
Bypasses the broken AgentScope GeminiChatModel wrapper (which uses the
deprecated google.generativeai SDK and passes messages in the wrong format).

Uses google-genai SDK directly with correct Content/Part message format.

Usage
-----
    from src.skills.gemini_direct import make_gemini_fn

    fn = make_gemini_fn(api_key="...", model="models/gemini-2.0-flash")
    # fn : Callable[[list[dict]], str]  (same interface as other model_fns)
    response_text = fn([{"role": "user", "content": "Hello"}])
"""

from __future__ import annotations

import os
import time
from typing import Callable

from loguru import logger

ModelFn = Callable[[list[dict]], str]

# Default model preference order (cheapest quota first)
DEFAULT_MODEL_PREFERENCE = [
    "models/gemini-2.0-flash",
    "models/gemini-2.0-flash-lite",
    "models/gemini-2.5-flash-lite",
    "models/gemini-2.5-flash",
    "models/gemini-2.5-flash-preview-05-20",
]


def _convert_messages(messages: list[dict]) -> list:
    """
    Convert OpenAI-style messages [{"role": ..., "content": ...}]
    to google-genai Content objects.
    """
    from google.genai import types

    contents = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        # google-genai uses "user" and "model" roles
        genai_role = "model" if role == "assistant" else "user"
        contents.append(
            types.Content(
                role=genai_role,
                parts=[types.Part(text=str(text))],
            )
        )
    return contents


def make_gemini_fn(
    api_key: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_retries: int = 2,
    retry_delay: float = 5.0,
) -> ModelFn | None:
    """
    Build a sync model_fn that calls Gemini via the google-genai SDK.

    Returns None if google-genai is not installed or the key is missing.
    Automatically probes models in DEFAULT_MODEL_PREFERENCE to find one
    that isn't rate-limited.

    Parameters
    ----------
    api_key : str | None
        Gemini API key. Defaults to GEMINI_API_KEY env var.
    model : str | None
        Specific model to use, or None to auto-select from preference list.
    temperature : float
        Generation temperature.
    max_retries : int
        Number of retries on transient errors (not quota errors).
    retry_delay : float
        Seconds to wait between retries.
    """
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        logger.warning("Gemini: no API key — returning None")
        return None

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
    except ImportError:
        logger.warning("Gemini: google-genai not installed — pip install google-genai")
        return None

    # ── Model selection ───────────────────────────────────────────────────────
    active_model: str | None = None

    if model:
        candidates = [model]
    else:
        candidates = DEFAULT_MODEL_PREFERENCE

    # Probe to find a non-rate-limited model (1-token probe)
    for candidate in candidates:
        try:
            client.models.generate_content(
                model=candidate,
                contents="ping",
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=1,
                ),
            )
            active_model = candidate
            logger.info(f"Gemini: selected model {candidate}")
            break
        except Exception as exc:
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                logger.debug(f"Gemini: {candidate} rate-limited, skipping")
            elif "404" in err or "NOT_FOUND" in err:
                logger.debug(f"Gemini: {candidate} not found, skipping")
            else:
                logger.debug(f"Gemini: {candidate} error: {err[:60]}")

    if active_model is None:
        logger.warning("Gemini: all models rate-limited or unavailable — returning None")
        return None

    # ── Build the callable ────────────────────────────────────────────────────
    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=2048,
    )

    def _call(messages: list[dict]) -> str:
        contents = _convert_messages(messages)
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                resp = client.models.generate_content(
                    model=active_model,
                    contents=contents,
                    config=config,
                )
                text = resp.text or ""
                return text
            except Exception as exc:
                err = str(exc)
                last_exc = exc
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    # Quota exhausted — don't retry, raise immediately
                    raise
                if attempt < max_retries:
                    logger.debug(f"Gemini call attempt {attempt+1} failed: {err[:60]} — retrying")
                    time.sleep(retry_delay)
        raise last_exc  # type: ignore[misc]

    return _call


def probe_quota(api_key: str | None = None) -> dict:
    """
    Quick diagnostic: test each model and return a status dict.
    Useful for pre-flight checks before a benchmark run.
    """
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    results = {}
    if not key:
        return {"error": "no_api_key"}

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
    except ImportError:
        return {"error": "google_genai_not_installed"}

    for candidate in DEFAULT_MODEL_PREFERENCE:
        try:
            t0 = time.monotonic()
            client.models.generate_content(
                model=candidate,
                contents="ping",
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=1),
            )
            results[candidate] = {"status": "ok", "latency_ms": round((time.monotonic()-t0)*1000, 1)}
        except Exception as exc:
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                results[candidate] = {"status": "quota_exhausted"}
            elif "404" in err:
                results[candidate] = {"status": "not_found"}
            else:
                results[candidate] = {"status": "error", "detail": err[:80]}

    available = [k for k, v in results.items() if v.get("status") == "ok"]
    results["_summary"] = {
        "available": available,
        "any_available": len(available) > 0,
    }
    return results
