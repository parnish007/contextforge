"""
ContextForge v3.0 — PM Agent (Phase 2)

The Project Manager agent owns the tasks table.  It does not touch code.

Responsibilities:
  - Break high-level goals into 3-5 concrete tasks (LLM or rule-based)
  - Persist tasks to SQLite via StorageAdapter
  - Report completion % and current sprint for the dashboard

Actions handled in reply() / metadata:
  plan_goal   — {"action": "plan_goal", "goal": str, "sprint": str}
  list_tasks  — {"action": "list_tasks", "status": str | None}
  get_stats   — {"action": "get_stats"}
  update_task — {"action": "update_task", "task_id": str, "status": str}
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Callable

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# System prompt for goal decomposition
# ---------------------------------------------------------------------------

_PLAN_PROMPT = """\
You are the Project Manager for ContextForge.
Given a high-level goal, decompose it into 3 to 5 concrete, actionable tasks.

Output a JSON array of task objects with EXACTLY these fields:
  title       : str  — concise action title (≤10 words)
  description : str  — one sentence describing what to do
  priority    : int  — 1 (highest) to 5 (lowest)
  assigned_to : str  — one of: GhostCoder, Researcher, PM, Librarian
  sprint      : str  — sprint label, e.g. "Phase 2 Sprint 1"

Rules:
- Output ONLY a valid JSON array. No markdown, no prose.
- Tasks must be specific and independently actionable.
- At least one task should be assigned to Researcher for background research.
"""


# ---------------------------------------------------------------------------
# PMAgent
# ---------------------------------------------------------------------------

class PMAgent(AgentBase):
    """
    Phase 2 — Project Manager.

    Parameters
    ----------
    name : str
    model_fn : Callable[[list[dict]], str] | None
        LLM wrapper (same pattern as GhostCoder).  None = rule-based fallback.
    storage : StorageAdapter
        Shared database adapter.
    project_id : str
    """

    def __init__(
        self,
        name: str = "PM",
        model_fn: Callable[[list[dict]], str] | None = None,
        storage: StorageAdapter | None = None,
        project_id: str | None = None,
    ):
        super().__init__()
        self.name = name
        self._model_fn = model_fn
        self._storage = storage
        self._project_id = project_id or os.getenv("PROJECT_ID", "default")
        logger.info(
            f"PM initialised — project={self._project_id}, "
            f"model={'llm' if model_fn else 'rule-based'}"
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

        if action == "plan_goal":
            return await self._handle_plan_goal(payload)
        if action == "list_tasks":
            return self._handle_list_tasks(payload)
        if action == "get_stats":
            return self._handle_get_stats()
        if action == "update_task":
            return self._handle_update_task(payload)

        return self._noop()

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_plan_goal(self, payload: dict) -> Msg:
        goal = payload.get("goal", "").strip()
        sprint = payload.get("sprint", f"Phase 2 Sprint 1")
        if not goal:
            return self._error("plan_goal requires 'goal' field")

        tasks = self._decompose_goal(goal, sprint)
        saved_ids: list[str] = []

        if self._storage:
            for t in tasks:
                tid = self._storage.upsert_task({
                    **t,
                    "project_id": self._project_id,
                    "parent_goal": goal,
                    "created_by_agent": self.name,
                })
                saved_ids.append(tid)
                logger.info(
                    f"PM: task [{t['priority']}] '{t['title'][:60]}' → {tid[:8]}"
                )
        else:
            logger.warning("PM: no StorageAdapter wired — tasks not persisted")

        return Msg(
            self.name,
            content=f"PM: planned {len(tasks)} task(s) for goal: {goal[:60]}",
            role="assistant",
            metadata={
                "action": "tasks_planned",
                "goal": goal,
                "tasks": tasks,
                "saved_ids": saved_ids,
            },
        )

    def _handle_list_tasks(self, payload: dict) -> Msg:
        status_filter = payload.get("status")
        if self._storage:
            tasks = self._storage.list_tasks(self._project_id, status=status_filter, limit=20)
        else:
            tasks = []
        return Msg(
            self.name,
            content=f"PM: {len(tasks)} task(s) returned",
            role="assistant",
            metadata={"action": "tasks_listed", "tasks": tasks},
        )

    def _handle_get_stats(self) -> Msg:
        if self._storage:
            stats = self._storage.get_task_stats(self._project_id)
        else:
            stats = {
                "total": 0, "done": 0, "pending": 0,
                "in_progress": 0, "blocked": 0,
                "pct_complete": 0, "current_sprint": "",
            }
        return Msg(
            self.name,
            content=f"PM: {stats['pct_complete']}% complete ({stats['done']}/{stats['total']})",
            role="assistant",
            metadata={"action": "stats", **stats},
        )

    def _handle_update_task(self, payload: dict) -> Msg:
        task_id = payload.get("task_id", "")
        status = payload.get("status", "done")
        if self._storage and task_id:
            self._storage.update_task_status(task_id, status)
            logger.info(f"PM: task {task_id[:8]} → {status}")
        return Msg(
            self.name,
            content=f"PM: task {task_id[:8]} updated to {status}",
            role="assistant",
            metadata={"action": "task_updated", "task_id": task_id, "status": status},
        )

    # ------------------------------------------------------------------
    # Goal decomposition
    # ------------------------------------------------------------------

    def _decompose_goal(self, goal: str, sprint: str) -> list[dict]:
        """LLM decomposition with rule-based fallback."""
        if self._model_fn:
            try:
                raw = self._model_fn([
                    {"role": "system", "content": _PLAN_PROMPT},
                    {"role": "user", "content": f"Goal: {goal}\nSprint: {sprint}"},
                ])
                tasks = self._parse_tasks(raw, sprint)
                if tasks:
                    return tasks
            except Exception as exc:
                logger.warning(f"PM: LLM decomposition failed ({exc}) — using fallback")

        return self._fallback_tasks(goal, sprint)

    def _parse_tasks(self, raw: str, sprint: str) -> list[dict]:
        """Strip markdown fences and parse JSON array from LLM output."""
        text = raw.strip()
        if "```" in text:
            start = text.find("[", text.find("```"))
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        else:
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            tasks = json.loads(text)
        except Exception:
            return []
        result = []
        for t in tasks:
            if isinstance(t, dict) and t.get("title"):
                result.append({
                    "title": str(t.get("title", ""))[:120],
                    "description": str(t.get("description", ""))[:300],
                    "priority": int(t.get("priority", 3)),
                    "assigned_to": str(t.get("assigned_to", "GhostCoder")),
                    "sprint": str(t.get("sprint", sprint)),
                    "status": "pending",
                })
        return result[:5]

    def _fallback_tasks(self, goal: str, sprint: str) -> list[dict]:
        """Rule-based task generation for any goal string."""
        keywords = goal.lower()
        base: list[dict] = [
            {
                "title": f"Research: {goal[:50]}",
                "description": f"Research background and best practices for: {goal}",
                "priority": 1,
                "assigned_to": "Researcher",
                "sprint": sprint,
                "status": "pending",
            },
            {
                "title": f"Design architecture for: {goal[:40]}",
                "description": "Define components, interfaces, and data flow",
                "priority": 2,
                "assigned_to": "GhostCoder",
                "sprint": sprint,
                "status": "pending",
            },
            {
                "title": f"Implement: {goal[:50]}",
                "description": f"Write the core implementation for: {goal}",
                "priority": 3,
                "assigned_to": "GhostCoder",
                "sprint": sprint,
                "status": "pending",
            },
        ]
        if any(k in keywords for k in ["test", "validate", "verify", "rag", "llm"]):
            base.append({
                "title": "Write integration tests",
                "description": "Validate end-to-end behaviour and edge cases",
                "priority": 4,
                "assigned_to": "GhostCoder",
                "sprint": sprint,
                "status": "pending",
            })
        if any(k in keywords for k in ["doc", "spec", "omega", "update"]):
            base.append({
                "title": "Update CLAUDE.md and OMEGA_SPEC",
                "description": "Sync documentation with implementation decisions",
                "priority": 5,
                "assigned_to": "PM",
                "sprint": sprint,
                "status": "pending",
            })
        return base[:5]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})

    def _error(self, msg: str) -> Msg:
        logger.error(f"PM: {msg}")
        return Msg(self.name, content=msg, role="assistant", metadata={"action": "error", "detail": msg})
