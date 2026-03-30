"""
ContextForge v3.0 — Coder Agent (Phase 3: Builder)

The Coder closes the loop between PM tasks and the Knowledge Graph.

Pipeline per execution:
  1. Fetch the task from StorageAdapter by task_id
  2. Run ContextRAG to retrieve L1→L2→L3 context from Librarian
  3. Call LLM (or rule-based fallback) with the Plan-and-Execute prompt
  4. Parse the structured response: plan + code_block + rationale
  5. Shadow-Reviewer gate: semantic match + contradiction check
     APPROVED        → write node as 'active', mark task done
     REVISION_NEEDED → write node as 'pending', task stays in_progress
     BLOCKED         → discard node, task stays in_progress
  6. Write decision_node via Librarian (status set by reviewer verdict)
  7. Mark task done/in_progress per verdict

Constraint (per spec): every generated code block must include a
# RATIONALE: <text> comment so the Sentry can track it as a valid decision.

Actions handled in reply() / metadata:
  execute_task — {"action": "execute_task", "task_id": str}
  plan_only    — {"action": "plan_only",    "task_id": str}
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Callable, TYPE_CHECKING

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

from src.skills.context_rag import ContextRAG

if TYPE_CHECKING:
    from src.agents.librarian import LibrarianAgent
    from src.agents.reviewer import ShadowReviewer
    from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PLAN_EXECUTE_PROMPT = """\
You are the Coder agent for ContextForge. You receive a task and relevant \
context nodes, then produce a Plan-and-Execute block.

Output ONLY valid JSON with these fields:
  plan        : list[str]   — numbered steps (3-5 items)
  code_block  : str         — complete, runnable Python code
  rationale   : str         — why this approach was chosen (1-2 sentences)
  area        : str         — implementation area (e.g. "error-handling", "rag")
  confidence  : float       — 0.0-1.0

CRITICAL constraint: the code_block MUST begin with a comment line:
  # RATIONALE: <one-line rationale>
This allows the Sentry file-watcher to detect it as a tracked decision.

No markdown outside the JSON. No extra prose. Only the JSON object.
"""

_FALLBACK_CODE_TEMPLATE = """\
# RATIONALE: {rationale}
# Task: {title}
# Context: Retrieved from L{tier} cache ({node_count} node(s))
# Sprint: {sprint}

def implement_{slug}():
    \"\"\"
    Implementation stub for: {title}

    Plan:
{plan_lines}

    References:
{ref_lines}
    \"\"\"
    # TODO: implement based on context above
    raise NotImplementedError("{title}")
"""


# ---------------------------------------------------------------------------
# CoderAgent
# ---------------------------------------------------------------------------

class CoderAgent(AgentBase):
    """
    Phase 3 — The Builder.

    Parameters
    ----------
    name : str
    model_fn : Callable[[list[dict]], str] | None
        LLM wrapper (same pattern as GhostCoder / PM).  None = stub fallback.
    librarian : LibrarianAgent | None
        For L1 cache reads/writes via ContextRAG.
    storage : StorageAdapter | None
        For task lookup and status updates + node persistence.
    project_id : str | None
    """

    def __init__(
        self,
        name: str = "Coder",
        model_fn: Callable[[list[dict]], str] | None = None,
        librarian: "LibrarianAgent | None" = None,
        storage: "StorageAdapter | None" = None,
        project_id: str | None = None,
        reviewer: "ShadowReviewer | None" = None,
    ):
        super().__init__()
        self.name = name
        self._model_fn = model_fn
        self._librarian = librarian
        self._storage = storage
        self._project_id = project_id or os.getenv("PROJECT_ID", "default")
        self._reviewer = reviewer
        self._rag = ContextRAG(
            librarian=librarian,
            storage=storage,
            db_path=os.getenv("DB_PATH", "data/contextforge.db"),
        )
        logger.info(
            f"Coder initialised — project={self._project_id}, "
            f"model={'llm' if model_fn else 'stub-fallback'}, "
            f"reviewer={'wired' if reviewer else 'none'}"
        )

    # ------------------------------------------------------------------
    # AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        if x is None:
            return self._noop()
        payload: dict = {}
        if x.metadata and isinstance(x.metadata, dict):
            payload = x.metadata
        elif isinstance(x.content, str):
            try:
                payload = json.loads(x.content)
            except Exception:
                return self._noop()

        action = payload.get("action", "")
        if action in ("execute_task", "plan_only"):
            return await self._handle_execute(payload, dry_run=(action == "plan_only"))

        return self._noop()

    # ------------------------------------------------------------------
    # Sync entry point (mirrors GhostCoder.process_batch pattern)
    # ------------------------------------------------------------------

    def execute(self, task_id: str, dry_run: bool = False) -> dict:
        """Sync entry point for Director loop."""
        import asyncio
        msg = asyncio.run(
            self._handle_execute(
                {"action": "execute_task", "task_id": task_id},
                dry_run=dry_run,
            )
        )
        return msg.metadata or {}

    # ------------------------------------------------------------------
    # Core handler
    # ------------------------------------------------------------------

    async def _handle_execute(self, payload: dict, dry_run: bool = False) -> Msg:
        task_id = payload.get("task_id", "").strip()
        if not task_id:
            return self._error("execute_task requires 'task_id'")

        # ── Step 1: Fetch task ────────────────────────────────────────
        task = self._fetch_task(task_id)
        if not task:
            return self._error(f"task_id '{task_id[:8]}' not found")

        title = task.get("title", "Unknown task")
        description = task.get("description", title)
        sprint = task.get("sprint", "")
        logger.info(f"Coder: executing task '{title[:60]}' [{task_id[:8]}]")

        # ── Step 2: Retrieve context via RAG ──────────────────────────
        rag_query = f"{title} {description}"
        rag_bundle = self._rag.retrieve(rag_query, project_id=self._project_id)
        logger.info(
            f"Coder: RAG bundle tier={rag_bundle.tier} "
            f"tokens~={rag_bundle.token_estimate} "
            f"nodes={len(rag_bundle.node_ids)}"
        )

        # ── Step 3: Generate Plan-and-Execute ─────────────────────────
        result = self._generate(task, rag_bundle)

        # ── Step 4: Shadow-Reviewer gate ──────────────────────────────
        verdict = None
        verdict_str = "APPROVED"
        node_id: str | None = None

        if self._reviewer is not None and not dry_run:
            node_candidate = self._build_node(task, result, rag_bundle)
            verdict = self._reviewer.review(node_candidate, task)
            verdict_str = verdict.verdict
            logger.info(
                f"Coder: Reviewer says {verdict_str} "
                f"(score={verdict.semantic_score:.2f})"
            )

            if verdict_str == "BLOCKED":
                logger.error(
                    f"Coder: BLOCKED by Reviewer — {verdict.notes[:100]}"
                )
                return Msg(
                    self.name,
                    content=f"BLOCKED: {verdict.notes[:120]}",
                    role="assistant",
                    metadata={
                        "action": "blocked",
                        "task_id": task_id,
                        "verdict": verdict_str,
                        "contradiction_detail": verdict.contradiction_detail,
                        "notes": verdict.notes,
                        "rag_tier": rag_bundle.tier,
                    },
                )

            # Adjust node status per verdict
            if verdict_str == "REVISION_NEEDED":
                node_candidate["status"] = "pending"
            # verdict == "APPROVED" keeps status="active" (set in _build_node)

            # Write to Librarian
            if self._librarian is not None:
                write_msg = Msg(
                    self.name,
                    content=f"write_node: {node_candidate['id'][:8]}",
                    role="assistant",
                    metadata={"action": "write_node", "node": node_candidate},
                )
                await self._librarian.reply(write_msg)
                node_id = node_candidate["id"]
                logger.info(f"Coder: node {node_id[:8]} ({node_candidate['status']}) → Librarian")

        elif not dry_run and self._librarian is not None:
            # No reviewer wired — write directly as before
            node_candidate = self._build_node(task, result, rag_bundle)
            write_msg = Msg(
                self.name,
                content=f"write_node: {node_candidate['id'][:8]}",
                role="assistant",
                metadata={"action": "write_node", "node": node_candidate},
            )
            await self._librarian.reply(write_msg)
            node_id = node_candidate["id"]
            logger.info(f"Coder: node {node_id[:8]} → Librarian (no reviewer)")

        elif dry_run:
            logger.info("Coder: dry_run=True — skipping Librarian write")

        # ── Step 5: Mark task status based on verdict ─────────────────
        if not dry_run and self._storage:
            if verdict_str == "APPROVED":
                self._storage.update_task_status(task_id, "done")
                logger.info(f"Coder: task {task_id[:8]} → done")
            elif verdict_str == "REVISION_NEEDED":
                self._storage.update_task_status(task_id, "in_progress")
                logger.info(f"Coder: task {task_id[:8]} → in_progress (revision needed)")

        summary_line = (
            f"Coder: '{title[:50]}' | "
            f"verdict={verdict_str} tier={rag_bundle.tier} "
            f"plan={len(result['plan'])} node={node_id[:8] if node_id else 'n/a'}"
        )
        logger.info(summary_line)

        return Msg(
            self.name,
            content=summary_line,
            role="assistant",
            metadata={
                "action": "task_executed",
                "task_id": task_id,
                "task_title": title,
                "plan": result["plan"],
                "code_block": result["code_block"],
                "rationale": result["rationale"],
                "area": result["area"],
                "confidence": result["confidence"],
                "rag_tier": rag_bundle.tier,
                "rag_nodes": rag_bundle.node_ids,
                "node_id": node_id,
                "verdict": verdict_str,
                "reviewer_score": verdict.semantic_score if verdict else None,
                "dry_run": dry_run,
            },
        )

    # ------------------------------------------------------------------
    # Code generation
    # ------------------------------------------------------------------

    def _generate(self, task: dict, rag_bundle) -> dict:
        """LLM generation with structured fallback."""
        if self._model_fn:
            try:
                user_msg = self._build_user_message(task, rag_bundle)
                raw = self._model_fn([
                    {"role": "system", "content": _PLAN_EXECUTE_PROMPT},
                    {"role": "user", "content": user_msg},
                ])
                parsed = self._parse_response(raw)
                if parsed:
                    # Enforce RATIONALE comment in code_block
                    parsed["code_block"] = self._inject_rationale(
                        parsed["code_block"], parsed.get("rationale", "")
                    )
                    return parsed
            except Exception as exc:
                logger.warning(f"Coder: LLM generation failed ({exc}) — fallback")

        return self._fallback_generate(task, rag_bundle)

    def _build_user_message(self, task: dict, rag_bundle) -> str:
        lines = [
            f"Task ID: {task.get('id', '?')[:8]}",
            f"Title: {task.get('title', '')}",
            f"Description: {task.get('description', '')}",
            f"Sprint: {task.get('sprint', '')}",
            f"",
            f"## Retrieved Context ({rag_bundle.tier}, ~{rag_bundle.token_estimate} tokens)",
            rag_bundle.content[:2000],  # keep prompt lean
        ]
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> dict | None:
        text = raw.strip()
        # Strip markdown fences
        if "```" in text:
            start = text.find("{", text.find("```"))
            end = text.rfind("}") + 1
        else:
            start = text.find("{")
            end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end])
        except Exception:
            return None
        if not obj.get("code_block"):
            return None
        return {
            "plan": [str(s) for s in obj.get("plan", [])][:5],
            "code_block": str(obj.get("code_block", "")),
            "rationale": str(obj.get("rationale", ""))[:300],
            "area": str(obj.get("area", "implementation")),
            "confidence": float(obj.get("confidence", 0.70)),
        }

    def _inject_rationale(self, code: str, rationale: str) -> str:
        """Ensure the code starts with # RATIONALE: comment."""
        line = f"# RATIONALE: {rationale[:120]}"
        if code.startswith("# RATIONALE:"):
            return code
        return line + "\n" + code

    def _fallback_generate(self, task: dict, rag_bundle) -> dict:
        """Rule-based code stub when no LLM is available."""
        title = task.get("title", "untitled")
        description = task.get("description", title)
        sprint = task.get("sprint", "")
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower())[:30].strip("_")

        plan = [
            f"1. Review context from {rag_bundle.tier} cache ({len(rag_bundle.node_ids)} node(s))",
            f"2. Identify inputs and outputs for: {title[:50]}",
            f"3. Implement core logic with error handling",
            f"4. Add rationale comment for Sentry tracking",
            f"5. Test and validate implementation",
        ]

        plan_lines = "\n".join(f"      {p}" for p in plan)
        refs = rag_bundle.sources[:3] if rag_bundle.sources else ["(no prior context)"]
        ref_lines = "\n".join(f"      - {r}" for r in refs)

        code_block = _FALLBACK_CODE_TEMPLATE.format(
            rationale=description[:100],
            title=title,
            tier=rag_bundle.tier,
            node_count=len(rag_bundle.node_ids),
            sprint=sprint,
            slug=slug,
            plan_lines=plan_lines,
            ref_lines=ref_lines,
        )

        return {
            "plan": plan,
            "code_block": code_block,
            "rationale": f"Stub generated for: {description[:100]}",
            "area": "implementation",
            "confidence": 0.40,
        }

    # ------------------------------------------------------------------
    # Node builder
    # ------------------------------------------------------------------

    def _build_node(self, task: dict, result: dict, rag_bundle) -> dict:
        now = datetime.utcnow().isoformat()
        return {
            "id": str(uuid.uuid4()),
            "project_id": self._project_id,
            "summary": f"[Coder] {task.get('title', '')[:200]}",
            "rationale": result["rationale"],
            "area": result["area"],
            "alternatives": [],
            "dependencies": rag_bundle.node_ids,
            "triggered_by": f"coder.execute_task.{task.get('id', '')[:8]}",
            "confidence": result["confidence"],
            "importance": 0.8,
            "vclock": {},
            "origin_client": "coder",
            "tombstone": False,
            "created_by_agent": self.name,
            "validated_by": "",
            "audited_by": "",
            "status": "active",
            "type_metadata": {
                "task_id": task.get("id", ""),
                "task_title": task.get("title", ""),
                "plan": result["plan"],
                "rag_tier": rag_bundle.tier,
                "rag_nodes": rag_bundle.node_ids,
                "code_preview": result["code_block"][:300],
                "sprint": task.get("sprint", ""),
            },
            "created_at": now,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # Task fetch helper
    # ------------------------------------------------------------------

    def _fetch_task(self, task_id: str) -> dict | None:
        """Look up a task from StorageAdapter (prefix match supported)."""
        if not self._storage:
            return None
        try:
            # Try exact match first
            tasks = self._storage.list_tasks(self._project_id, status=None, limit=100)
            for t in tasks:
                if t["id"] == task_id or t["id"].startswith(task_id):
                    return t
        except Exception as exc:
            logger.debug(f"Coder: task fetch failed — {exc}")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})

    def _error(self, msg: str) -> Msg:
        logger.error(f"Coder: {msg}")
        return Msg(self.name, content=msg, role="assistant", metadata={"action": "error", "detail": msg})


# lazy import needed inside fallback
import re
