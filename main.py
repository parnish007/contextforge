"""
ContextForge v3.0 — Entry Point  (Phase 1 Complete)
The Omega Persistence Engine

Phase 1 pipeline:
  Sentry → GhostCoder (LLM distiller) → HITL Gate → Librarian → SQLite

LLM priority (set keys in .env to activate):
  1. Gemini Flash   — GEMINI_API_KEY  (recommended: free tier, long context)
  2. Groq Llama     — GROQ_API_KEY    (alternative: free tier, very fast)
  3. Ollama local   — OLLAMA_URL      (local, zero cost, requires running server)
  4. Rule-based fallback (no key needed, always works)

Dashboard:
  An Omega-Pulse Rich terminal panel prints on startup and auto-refreshes
  every DASHBOARD_REFRESH seconds (default 10).

Run:
  python main.py
  python main.py --no-dashboard     # suppress the Rich panel
  python main.py --hitl-off         # auto-approve all nodes (batch testing)
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

import agentscope
from dotenv import load_dotenv
from loguru import logger

from src.agents.ghost_coder import GhostCoderAgent
from src.agents.librarian import LibrarianAgent
from src.agents.sentry import SentryAgent
from src.core.dashboard import OmegaDashboard, set_agent_online
from src.core.signals import SignalBatch

load_dotenv()


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ContextForge v3.0")
    p.add_argument("--no-dashboard", action="store_true", help="Suppress Rich dashboard")
    p.add_argument("--hitl-off", action="store_true", help="Auto-approve all nodes")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Model spec builders  (AgentScope 1.0.18 — direct instantiation)
# ---------------------------------------------------------------------------

def _gemini_spec() -> dict | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    return {
        "model_type": "gemini_chat",
        "model_name": "gemini-2.5-flash-preview-05-20",
        "api_key": key,
        "temperature": 0.2,
    }


def _groq_spec() -> dict | None:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    return {
        "model_type": "openai_chat",
        "model_name": "llama-3.3-70b-versatile",
        "api_key": key,
        "base_url": "https://api.groq.com/openai/v1",
        "temperature": 0.3,
    }


def _ollama_spec() -> dict:
    return {
        "model_type": "ollama_chat",
        "model_name": os.getenv("OLLAMA_MODEL", "llama3.3"),
        "base_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
    }


def _pick_model_spec() -> tuple[dict | None, str]:
    """
    Return (spec_dict, label) for the highest-priority available LLM.
    Gemini → Groq → Ollama → None (rule-based fallback).
    """
    for spec, label in [
        (_gemini_spec(), "Gemini Flash (free tier)"),
        (_groq_spec(),   "Groq / Llama-3.3-70B (free tier)"),
    ]:
        if spec:
            return spec, label
    # Ollama is always tried; GhostCoder handles connection errors gracefully
    return _ollama_spec(), "Ollama (local)"


# ---------------------------------------------------------------------------
# Pipeline callback
# ---------------------------------------------------------------------------

def _make_pipeline_callback(ghost_coder: GhostCoderAgent):
    def _on_batch(batch: SignalBatch) -> None:
        ghost_coder.process_batch(batch.model_dump())
    return _on_batch


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # Override HITL auto-approve via CLI flag
    if args.hitl_off:
        os.environ["HITL_AUTO_APPROVE"] = "true"

    logger.info("ContextForge v3.0 — starting Phase 1 pipeline")

    # ── AgentScope init (1.0.18) ──────────────────────────────────────
    agentscope.init(project="ContextForge", logging_level="INFO")

    # ── Librarian ─────────────────────────────────────────────────────
    db_path = os.getenv("DB_PATH", "data/contextforge.db")
    librarian = LibrarianAgent(name="Librarian", db_path=db_path)
    set_agent_online("Librarian")
    logger.info(f"Librarian online — db={db_path}")

    # ── Ghost-Coder ───────────────────────────────────────────────────
    model_spec, model_label = _pick_model_spec()
    ghost_coder = GhostCoderAgent(
        name="GhostCoder",
        model_spec=model_spec,
        project_type=os.getenv("PROJECT_TYPE", "code"),
        project_id=os.getenv("PROJECT_ID"),
        librarian=librarian,
    )
    set_agent_online("GhostCoder")
    logger.info(f"GhostCoder online — LLM: {model_label}")

    # ── Sentry ────────────────────────────────────────────────────────
    sentry = SentryAgent(
        name="Sentry",
        watch_path=os.getenv("SENTRY_WATCH_PATH", "."),
        batch_threshold=int(os.getenv("SENTRY_BATCH_THRESHOLD", "3")),
        pause_timeout=int(os.getenv("SENTRY_PAUSE_TIMEOUT", "30")),
        project_id=os.getenv("PROJECT_ID"),
    )
    pipeline_cb = _make_pipeline_callback(ghost_coder)
    _orig = sentry._broadcast

    def _patched(batch: SignalBatch) -> None:
        _orig(batch)
        pipeline_cb(batch)

    sentry._broadcast = _patched  # type: ignore[method-assign]
    sentry.start_watching()
    set_agent_online("Sentry")
    logger.info(
        "Sentry online — watching: "
        + os.path.abspath(os.getenv("SENTRY_WATCH_PATH", "."))
    )

    # ── Dashboard ─────────────────────────────────────────────────────
    dashboard = OmegaDashboard(
        librarian=librarian,
        db_path=db_path,
        refresh_seconds=int(os.getenv("DASHBOARD_REFRESH", "10")),
        project_id=os.getenv("PROJECT_ID"),
    )

    # ── Graceful shutdown ─────────────────────────────────────────────
    def _shutdown(sig: int, frame) -> None:  # type: ignore[type-arg]
        logger.info("ContextForge: shutdown signal received")
        sentry.stop_watching()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Banner ────────────────────────────────────────────────────────
    hitl_threshold = float(os.getenv("HITL_CONFIDENCE_THRESHOLD", "0.70"))
    hitl_auto = os.getenv("HITL_AUTO_APPROVE", "false").lower() == "true"

    logger.info(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  ContextForge v3.0 — Phase 1 Complete\n"
        "  Sentry ✓  GhostCoder ✓  Librarian ✓  HITL ✓\n"
        f"  LLM  : {model_label}\n"
        f"  DB   : {db_path}\n"
        f"  HITL : threshold={hitl_threshold}  auto={hitl_auto}\n"
        "  Save any .py / .md / .json file to trigger the pipeline.\n"
        "  Press Ctrl+C to stop.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # ── Initial dashboard render ──────────────────────────────────────
    if not args.no_dashboard:
        dashboard.render()

    # ── Observation loop ──────────────────────────────────────────────
    refresh = int(os.getenv("DASHBOARD_REFRESH", "10"))
    last_refresh = time.monotonic()

    try:
        while True:
            time.sleep(1)
            if not args.no_dashboard:
                now = time.monotonic()
                if now - last_refresh >= refresh:
                    dashboard.render()
                    last_refresh = now
    except KeyboardInterrupt:
        _shutdown(0, None)


if __name__ == "__main__":
    main()
