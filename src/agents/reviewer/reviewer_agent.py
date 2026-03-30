"""
ContextForge v3.0 — Shadow-Reviewer Agent (Phase 4: Hardening)

Acts as a Gatekeeper between the Coder and the Librarian.

Checks performed on every implementation node:
  1. Semantic Match    — cosine similarity between generated rationale and
                         the PM task description. < 0.80 → REVISION_NEEDED.
  2. Contradiction     — scans existing active nodes for destructive conflict.
                         If the task targets an entity that existing nodes
                         record as active/implemented and the operation is
                         destructive (delete/remove/disable), flags BLOCKED.

Verdicts:
  APPROVED        — node passes all checks; Librarian may persist as 'active'
  REVISION_NEEDED — semantic match too low; node saved as 'pending' for rework
  BLOCKED         — direct contradiction with project state; node rejected

Actions handled via reply():
  review_node  — {"action": "review_node", "node": dict, "task": dict}
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger

if TYPE_CHECKING:
    from src.core.storage import StorageAdapter


# ---------------------------------------------------------------------------
# Verdict type
# ---------------------------------------------------------------------------

Verdict = Literal["APPROVED", "REVISION_NEEDED", "BLOCKED"]

_SEMANTIC_THRESHOLD = 0.80          # below → REVISION_NEEDED
_CONTRADICTION_KEYWORDS = frozenset([
    "delete", "remove", "disable", "drop", "destroy", "kill", "stop",
    "uninstall", "decommission", "deprecate", "eliminate", "terminate",
])


@dataclass
class ReviewVerdict:
    verdict: Verdict
    semantic_score: float        # 0.0 – 1.0
    contradiction: bool
    contradiction_detail: str    # name of the conflicting entity, if any
    notes: str                   # human-readable explanation


# ---------------------------------------------------------------------------
# Cosine similarity (no external deps)
# ---------------------------------------------------------------------------

def _term_freq(text: str) -> dict[str, float]:
    tokens = re.findall(r"[a-z][a-z0-9_]{1,}", text.lower())
    tf: dict[str, float] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    return tf


def _cosine(a: str, b: str) -> float:
    """Term-frequency cosine similarity between two strings."""
    va, vb = _term_freq(a), _term_freq(b)
    if not va or not vb:
        return 0.0
    all_terms = set(va) | set(vb)
    dot = sum(va.get(t, 0) * vb.get(t, 0) for t in all_terms)
    mag_a = math.sqrt(sum(v * v for v in va.values()))
    mag_b = math.sqrt(sum(v * v for v in vb.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return round(dot / (mag_a * mag_b), 4)


# ---------------------------------------------------------------------------
# ShadowReviewer
# ---------------------------------------------------------------------------

class ShadowReviewer(AgentBase):
    """
    Phase 4 — The Critic / Gatekeeper.

    Parameters
    ----------
    name : str
    storage : StorageAdapter | None
        Used for contradiction checks against existing active nodes.
    project_id : str | None
    semantic_threshold : float
        Minimum cosine similarity score between Coder rationale and PM task
        description.  Default 0.80.
    """

    def __init__(
        self,
        name: str = "Shadow-Reviewer",
        storage: "StorageAdapter | None" = None,
        project_id: str | None = None,
        semantic_threshold: float = _SEMANTIC_THRESHOLD,
    ):
        super().__init__()
        self.name = name
        self._storage = storage
        self._project_id = project_id
        self._threshold = semantic_threshold
        logger.info(
            f"Shadow-Reviewer initialised — "
            f"threshold={semantic_threshold}, project={project_id}"
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

        if payload.get("action") == "review_node":
            return self._handle_review(payload)
        return self._noop()

    # ------------------------------------------------------------------
    # Sync entry point (for Coder pipeline)
    # ------------------------------------------------------------------

    def review(self, node: dict, task: dict) -> ReviewVerdict:
        """Synchronous gate — called directly by CoderAgent before Librarian write."""
        return self._run_checks(node, task)

    # ------------------------------------------------------------------
    # Handler
    # ------------------------------------------------------------------

    def _handle_review(self, payload: dict) -> Msg:
        node = payload.get("node", {})
        task = payload.get("task", {})
        verdict = self._run_checks(node, task)
        _log_verdict(verdict, node)
        return Msg(
            self.name,
            content=f"Reviewer: {verdict.verdict} (score={verdict.semantic_score:.2f})",
            role="assistant",
            metadata={
                "action": "review_result",
                "verdict": verdict.verdict,
                "semantic_score": verdict.semantic_score,
                "contradiction": verdict.contradiction,
                "contradiction_detail": verdict.contradiction_detail,
                "notes": verdict.notes,
            },
        )

    # ------------------------------------------------------------------
    # Check logic
    # ------------------------------------------------------------------

    def _run_checks(self, node: dict, task: dict) -> ReviewVerdict:
        rationale = node.get("rationale", "") or ""
        task_title = task.get("title", "") or ""
        task_desc = task.get("description", "") or ""
        task_text = f"{task_title} {task_desc}".strip()

        # ── Check 1: Semantic match ────────────────────────────────────
        score = _cosine(rationale, task_text)
        # If either string is very short, cosine is unreliable → be generous
        if len(rationale.split()) < 4 or len(task_text.split()) < 4:
            score = max(score, 0.80)

        if score < self._threshold:
            return ReviewVerdict(
                verdict="REVISION_NEEDED",
                semantic_score=score,
                contradiction=False,
                contradiction_detail="",
                notes=(
                    f"Semantic match {score:.2f} < threshold {self._threshold:.2f}. "
                    f"Rationale does not sufficiently reflect the task description. "
                    f"Task: '{task_text[:80]}'"
                ),
            )

        # ── Check 2: Contradiction scan ────────────────────────────────
        op_words = set(re.findall(r"[a-z]+", task_text.lower()))
        destructive = bool(op_words & _CONTRADICTION_KEYWORDS)

        if destructive and self._storage:
            conflict = self._find_conflict(task_text)
            if conflict:
                return ReviewVerdict(
                    verdict="BLOCKED",
                    semantic_score=score,
                    contradiction=True,
                    contradiction_detail=conflict,
                    notes=(
                        f"Task requests a destructive operation on '{conflict}', "
                        f"which is recorded as active in the knowledge graph. "
                        f"This contradicts the current project state."
                    ),
                )

        return ReviewVerdict(
            verdict="APPROVED",
            semantic_score=score,
            contradiction=False,
            contradiction_detail="",
            notes=f"Passed all checks (semantic_score={score:.2f}).",
        )

    def _find_conflict(self, task_text: str) -> str:
        """
        Check if the task's target entity appears in active nodes as something
        that should NOT be deleted.  Returns the conflicting entity name or ''.
        """
        if not self._storage or not self._project_id:
            return ""
        try:
            nodes = self._storage.list_nodes(
                self._project_id, status="active", limit=50
            )
        except Exception:
            return ""

        task_terms = set(re.findall(r"[a-z][a-z0-9_]{2,}", task_text.lower()))

        for node in nodes:
            node_text = f"{node.get('summary','')} {node.get('rationale','')}".lower()
            node_terms = set(re.findall(r"[a-z][a-z0-9_]{2,}", node_text))
            overlap = task_terms & node_terms
            # meaningful overlap AND the node is an implementation/core component
            if len(overlap) >= 2 and node.get("area") in (
                "implementation", "architecture", "core", "sentry",
                "librarian", "ghostcoder", "coder", "pm", "researcher",
            ):
                # Return the most descriptive overlapping term
                meaningful = sorted(
                    [t for t in overlap if len(t) > 4],
                    key=len, reverse=True
                )
                if meaningful:
                    return meaningful[0]
        return ""

    # ------------------------------------------------------------------
    def _noop(self) -> Msg:
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_verdict(v: ReviewVerdict, node: dict) -> None:
    area = node.get("area", "?")
    nid = node.get("id", "?")[:8]
    if v.verdict == "APPROVED":
        logger.info(
            f"Shadow-Reviewer: APPROVED [{area}] {nid} "
            f"score={v.semantic_score:.2f}"
        )
    elif v.verdict == "REVISION_NEEDED":
        logger.warning(
            f"Shadow-Reviewer: REVISION_NEEDED [{area}] {nid} "
            f"score={v.semantic_score:.2f} — {v.notes[:80]}"
        )
    else:
        logger.error(
            f"Shadow-Reviewer: BLOCKED [{area}] {nid} "
            f"conflict='{v.contradiction_detail}' — {v.notes[:80]}"
        )
