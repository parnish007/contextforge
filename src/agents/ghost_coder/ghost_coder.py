"""
ContextForge v3.0 — Agent 4: Ghost-Coder (The Worker)

First LLM-powered agent in the pipeline.  Receives a SignalBatch from the
Sentry (via MsgHub or direct callback), distils the raw file-change signals
into structured DecisionNode candidates, then hands them to the Librarian
for persistence.

No-LLM fallback: if no model is reachable the SemanticDistiller produces
rule-based node stubs (confidence ~0.4) so the pipeline never fully stalls.

Spec reference: OMEGA_SPEC.md §6.2 (GhostCoderAgent class definition).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

from src.agents.librarian import LibrarianAgent
from src.skills.distiller import SemanticDistiller
from src.skills.hitl_gate import HITLGate


# ---------------------------------------------------------------------------
# Skill-prompt loader (mirrors spec §6.2 _load_skill_prompt)
# ---------------------------------------------------------------------------

def _load_skill_prompt(agent: str, skill: str) -> str:
    """
    Load the active versioned skill prompt for `agent.skill`.

    Falls back to a minimal inline prompt if the manifest or file is missing
    so the agent can still operate during cold-start or partial init.
    """
    manifest_path = Path("prompts/manifest.json")
    if not manifest_path.exists():
        return _FALLBACK_SYS_PROMPT

    try:
        manifest = json.loads(manifest_path.read_text())
        version = manifest.get(f"{agent}.{skill}", "v1")
        prompt_path = Path(f"prompts/skills/{skill}/system.{version}.md")
        if prompt_path.exists():
            return prompt_path.read_text()
    except Exception as exc:
        logger.warning(f"GhostCoder: failed to load skill prompt '{agent}.{skill}' — {exc}")

    return _FALLBACK_SYS_PROMPT


_FALLBACK_SYS_PROMPT = (
    "You are Ghost-Coder, an agent that structures code changes into decision nodes. "
    "Output a JSON array of nodes with: summary, rationale, area, alternatives[], "
    "dependencies[], confidence (0-1), type_metadata.file_refs[], type_metadata.packages[]."
)

_SKILL_MAP = {
    "code": "code-architecture",
    "research": "research-synthesis",
    "study": "study-tracking",
    "general": "general-capture",
}


# ---------------------------------------------------------------------------
# GhostCoderAgent
# ---------------------------------------------------------------------------

class GhostCoderAgent(AgentBase):
    """
    Agent 4 — The Worker.

    Subscribes to ``batch_capture`` messages on the AgentScope MsgHub and
    runs the following pipeline per batch:

      1. SemanticDistiller calls the LLM (or falls back to rule-based) to
         produce candidate DecisionNode dicts.
      2. Each candidate is tagged with agent metadata and a provisional UUID.
      3. High-confidence nodes (≥ 0.5) are forwarded to the Librarian via a
         ``write_node`` message; low-confidence ones are marked ``pending``
         for future HITL review.

    Parameters
    ----------
    name : str
        AgentScope agent name.
    model_config_name : str | None
        Name of the AgentScope model config to use for the Distiller.
        If None the distiller runs in rule-based fallback mode.
    project_type : str
        One of "code" | "research" | "study" | "general".
    project_id : str | None
        Attached to every created node for downstream graph queries.
    librarian : LibrarianAgent | None
        Pre-instantiated Librarian to write nodes into.  Can be wired in
        later via ``set_librarian()``.
    """

    def __init__(
        self,
        name: str = "GhostCoder",
        model_spec: dict | None = None,
        project_type: str = "code",
        project_id: str | None = None,
        librarian: LibrarianAgent | None = None,
    ):
        """
        Parameters
        ----------
        model_spec : dict | None
            AgentScope 1.0.18 model specification dict, e.g.::

                {"model_type": "ollama_chat", "model_name": "llama3.3",
                 "base_url": "http://localhost:11434"}

            None → rule-based SemanticDistiller fallback (no LLM).
        """
        # AgentScope 1.0.18: AgentBase.__init__() takes no arguments.
        super().__init__()
        self.name = name

        self._project_type = project_type
        self._project_id = project_id or os.getenv("PROJECT_ID", "default")
        self._librarian = librarian
        self._hitl = HITLGate()

        # Build the distiller — bind the AgentScope model wrapper if available
        skill_name = _SKILL_MAP.get(project_type, "general-capture")
        self._sys_prompt = _load_skill_prompt("ghost_coder", skill_name)

        model_label = (
            f"{model_spec.get('model_type','?')}/{model_spec.get('model_name','?')}"
            if model_spec else "rule-based-fallback"
        )
        model_fn = self._make_model_fn(model_spec)
        self._distiller = SemanticDistiller(
            model_fn=model_fn,
            model_name=model_label,
            project_type=project_type,
        )

        logger.info(
            f"GhostCoder initialised — project_type={project_type}, "
            f"model={model_label}, skill={skill_name}"
        )

    # ------------------------------------------------------------------
    # Public wiring
    # ------------------------------------------------------------------

    def set_librarian(self, librarian: LibrarianAgent) -> None:
        """Inject the Librarian after construction (avoids circular imports)."""
        self._librarian = librarian

    # ------------------------------------------------------------------
    # AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        """
        Entry point for AgentScope MsgHub messages (async in 1.0.18).

        The structured payload is carried in Msg.metadata.
        """
        if x is None:
            return self._noop()

        payload: dict = {}
        if x.metadata and isinstance(x.metadata, dict):
            payload = x.metadata
        elif isinstance(x.content, str):
            import json as _json
            try:
                payload = _json.loads(x.content)
            except Exception:
                return self._noop()

        return await self._dispatch(payload)

    def process_batch(self, batch_dict: dict) -> dict:
        """
        Sync entry point for the pipeline callback in main.py.
        Runs the async pipeline in a new event loop.
        """
        import asyncio
        result_msg = asyncio.run(
            self._dispatch({"action": "batch_capture", "batch": batch_dict})
        )
        return result_msg.metadata or {}

    async def _dispatch(self, payload: dict) -> Msg:
        """Shared logic for both reply() and process_batch()."""
        action = payload.get("action", "")
        if action != "batch_capture":
            return self._noop()

        batch = payload.get("batch", {})
        signals: list[dict] = batch.get("signals", [])
        if not signals:
            return self._noop()

        logger.info(
            f"GhostCoder: received batch of {len(signals)} signal(s) "
            f"[batch_id={batch.get('batch_id', '?')[:8]}]"
        )

        return await self._process_batch(signals, batch)

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    async def _process_batch(self, signals: list[dict], batch_meta: dict) -> Msg:
        # Step 1: Distil signals → candidate nodes
        result = self._distiller.distill(signals)

        if result.used_fallback:
            logger.warning(
                f"GhostCoder: using rule-based fallback "
                f"({result.model_used})"
            )
        else:
            logger.info(
                f"GhostCoder: LLM distilled {len(result.nodes)} candidate(s) "
                f"via {result.model_used}"
            )

        # Step 2: Enrich each candidate with agent metadata
        enriched: list[dict] = []
        for raw_node in result.nodes:
            node = self._enrich_node(
                raw_node,
                batch_triggered_by=batch_meta.get("triggered_by", "threshold"),
            )
            enriched.append(node)
            self._log_node(node)

        # Step 3: HITL gate — review low-confidence nodes
        hitl_results = self._hitl.review_batch(enriched)

        # Step 4: Persist approved/edited nodes via Librarian
        persisted_ids: list[str] = []
        skipped_ids: list[str] = []

        for hr in hitl_results:
            node = hr.node
            node_id = node["id"]

            if hr.decision == "rejected":
                logger.info(f"GhostCoder: node {node_id[:8]} rejected by HITL")
                skipped_ids.append(node_id)
                continue

            if self._librarian is not None:
                write_msg = Msg(
                    self.name,
                    content=f"write_node: {node_id}",
                    role="assistant",
                    metadata={"action": "write_node", "node": node},
                )
                await self._librarian.reply(write_msg)
                persisted_ids.append(node_id)
            else:
                logger.warning(
                    "GhostCoder: no Librarian wired — node not persisted "
                    f"(id={node_id[:8]})"
                )
                skipped_ids.append(node_id)

        summary = (
            f"GhostCoder: processed {len(signals)} signal(s) → "
            f"{len(enriched)} node(s) | persisted={len(persisted_ids)} "
            f"skipped={len(skipped_ids)} | fallback={result.used_fallback}"
        )
        logger.info(summary)

        rejected_ids = [
            hr.node["id"] for hr in hitl_results if hr.decision == "rejected"
        ]
        result_payload = {
            "action": "nodes_created",
            "nodes": [hr.node for hr in hitl_results],
            "persisted_ids": persisted_ids,
            "skipped_ids": skipped_ids,
            "rejected_ids": rejected_ids,
            "used_fallback": result.used_fallback,
            "model_used": result.model_used,
        }
        return Msg(
            self.name,
            content=summary,
            role="assistant",
            metadata=result_payload,
        )

    # ------------------------------------------------------------------
    # Node enrichment
    # ------------------------------------------------------------------

    def _enrich_node(self, raw: dict, batch_triggered_by: str) -> dict:
        """Tag a raw distiller node with full agent + provenance metadata."""
        confidence = float(raw.get("confidence", 0.5))

        # HITL gate (spec §3.3 step 5)
        if confidence >= 0.85:
            status = "active"
        elif confidence >= 0.50:
            status = "pending"  # dashboard review
        else:
            status = "pending"  # low-confidence, also pending

        return {
            "id": str(uuid.uuid4()),
            "project_id": self._project_id,
            "summary": raw.get("summary", ""),
            "rationale": raw.get("rationale", ""),
            "area": raw.get("area", "general"),
            "alternatives": raw.get("alternatives", []),
            "dependencies": raw.get("dependencies", []),
            "triggered_by": f"sentry.{batch_triggered_by}",
            "confidence": confidence,
            "importance": raw.get("importance", 0.5),
            "vclock": {},
            "origin_client": "ghost_coder",
            "tombstone": False,
            "created_by_agent": self.name,
            "validated_by": "",
            "audited_by": "",
            "status": status,
            "type_metadata": raw.get("type_metadata", {}),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

    # ------------------------------------------------------------------
    # Model function factory
    # ------------------------------------------------------------------

    def _make_model_fn(
        self, model_spec: dict | None
    ):
        """
        Return a callable(messages) → str or None.

        AgentScope 1.0.18: models are instantiated directly from their class.
        `model_spec` is a dict with "model_type" and constructor kwargs.

        Falls back to None (rule-based distiller) on any error so the pipeline
        always has a path forward.
        """
        if not model_spec:
            return None

        model_type = model_spec.get("model_type", "")
        spec = {k: v for k, v in model_spec.items() if k != "model_type"}

        try:
            if model_type == "ollama_chat":
                from agentscope.model import OllamaChatModel
                model = OllamaChatModel(
                    model_name=spec.get("model_name", "llama3.3"),
                    host=spec.get("base_url"),
                )
            elif model_type == "openai_chat":
                from agentscope.model import OpenAIChatModel
                model = OpenAIChatModel(
                    model_name=spec.get("model_name", ""),
                    api_key=spec.get("api_key", ""),
                    base_url=spec.get("base_url"),
                    temperature=spec.get("temperature", 0.3),
                )
            elif model_type == "gemini_chat":
                from agentscope.model import GeminiChatModel
                model = GeminiChatModel(
                    model_name=spec.get("model_name", ""),
                    api_key=spec.get("api_key", ""),
                    temperature=spec.get("temperature", 0.2),
                )
            else:
                logger.warning(f"GhostCoder: unknown model_type '{model_type}'")
                return None

            def _call(messages: list[dict]) -> str:
                import asyncio
                full_messages = [
                    {"role": "system", "content": self._sys_prompt},
                ] + messages
                # AgentScope 1.0.18 models are async; run synchronously here
                response = asyncio.run(model(full_messages))
                # ChatResponse.content is a list of TextBlock / ToolUseBlock
                texts = [
                    block["text"]
                    for block in response.content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return "\n".join(texts)

            logger.info(
                f"GhostCoder: bound to model '{model_type}' "
                f"({spec.get('model_name', '?')})"
            )
            return _call

        except Exception as exc:
            logger.warning(
                f"GhostCoder: could not bind model ({exc}) — fallback mode"
            )
            return None

    # ------------------------------------------------------------------
    # Logging helper
    # ------------------------------------------------------------------

    def _log_node(self, node: dict) -> None:
        logger.info(
            f"  ↳ Node [{node['status'].upper()}] "
            f"area={node['area']} "
            f"confidence={node['confidence']:.2f} "
            f"| {node['summary'][:80]}"
        )

    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})
