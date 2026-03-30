"""
ContextForge v3.0 — Semantic Distiller Skill

Converts a raw SignalBatch (file paths + content hashes) into a human-readable
semantic summary by reading the actual file content and calling a low-cost LLM.

Design goals:
  • Keep token usage minimal — this runs on every Sentry batch.
  • Never block the pipeline — falls back to a rule-based summary if the LLM
    call fails or no model is configured.
  • Stay stateless — one call in, one DistillResult out.

Spec reference: OMEGA_SPEC.md §8 (Skills & Prompts Architecture).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DistillResult:
    """
    Structured output from the SemanticDistiller.

    Contains one candidate DecisionNode payload per significant signal,
    ready to be forwarded to GhostCoderAgent for final structuring or
    directly to the Librarian for persistence.
    """

    nodes: list[dict[str, Any]] = field(default_factory=list)
    raw_llm_output: str = ""
    used_fallback: bool = False
    model_used: str = "none"


# ---------------------------------------------------------------------------
# SemanticDistiller
# ---------------------------------------------------------------------------

class SemanticDistiller:
    """
    Skill that takes a batch of file-change signals and returns a DistillResult.

    Parameters
    ----------
    model_fn : Callable[[list[dict]], str] | None
        A callable that accepts a list of OpenAI-style chat messages and
        returns the assistant's reply text.  Typically bound to an
        AgentScope model wrapper's ``__call__``.  If None, the distiller
        operates in rule-based fallback mode only.
    model_name : str
        Label for logging / DistillResult.model_used.
    max_file_preview : int
        Maximum characters to include from each file's content preview.
        Keeps prompts small for low-cost models.
    project_type : str
        Selects the prompt template variant ("code" | "research" | "study" | "general").
    """

    def __init__(
        self,
        model_fn: Callable[[list[dict]], str] | None = None,
        model_name: str = "none",
        max_file_preview: int = 800,
        project_type: str = "code",
    ):
        self._model_fn = model_fn
        self._model_name = model_name
        self._max_file_preview = max_file_preview
        self._project_type = project_type

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def distill(self, signals: list[dict[str, Any]]) -> DistillResult:
        """
        Main entry point.  Accepts raw signal dicts (from SignalBatch.signals)
        and returns a DistillResult.
        """
        # Narrow to signals that are actual file paths we can read
        readable = [s for s in signals if s.get("file_path") and Path(s["file_path"]).exists()]

        if not readable:
            logger.debug("Distiller: no readable signals — using fallback")
            return self._fallback_result(signals)

        prompt_user = self._build_prompt(readable)

        if self._model_fn is None:
            logger.debug("Distiller: no model configured — using rule-based fallback")
            return self._fallback_result(signals)

        try:
            raw = self._model_fn(
                [{"role": "user", "content": prompt_user}]
            )
            nodes = self._parse_llm_output(raw, signals)
            logger.info(
                f"Distiller: LLM ({self._model_name}) produced "
                f"{len(nodes)} node candidate(s)"
            )
            return DistillResult(
                nodes=nodes,
                raw_llm_output=raw,
                used_fallback=False,
                model_used=self._model_name,
            )
        except Exception as exc:
            logger.warning(f"Distiller: LLM call failed ({exc}) — using fallback")
            return self._fallback_result(signals)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(self, signals: list[dict[str, Any]]) -> str:
        """Construct a compact, token-efficient prompt from the readable signals."""
        parts: list[str] = [
            "You are analysing code changes to extract architectural decisions.",
            "For each changed file below, produce a JSON array of decision nodes.",
            "Each node: {summary, rationale, area, alternatives[], dependencies[],"
            " confidence (0-1), type_metadata: {file_refs[], packages[]}}.",
            "Output ONLY the JSON array. No prose. No markdown fences.",
            "",
            "## Changed files",
        ]

        for sig in signals:
            path = sig.get("file_path", "")
            sig_type = sig.get("signal_type", "file_modify")
            preview = _read_preview(path, self._max_file_preview)
            parts.append(f"\n### {Path(path).name} ({sig_type})")
            parts.append(f"Path: {path}")
            if preview:
                parts.append(f"Content preview:\n{preview}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # LLM output parsing
    # ------------------------------------------------------------------

    def _parse_llm_output(
        self, raw: str, signals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Extract a list of node dicts from the LLM's response text.
        Tolerates markdown fences and partial JSON gracefully.
        """
        text = raw.strip()

        # Strip common markdown fences
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()

        # Try parsing as JSON array
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [_normalise_node(n, signals) for n in parsed if isinstance(n, dict)]
            if isinstance(parsed, dict):
                return [_normalise_node(parsed, signals)]
        except json.JSONDecodeError:
            pass

        # Try to extract the first JSON array from the text
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return [_normalise_node(n, signals) for n in parsed if isinstance(n, dict)]
            except json.JSONDecodeError:
                pass

        logger.warning("Distiller: could not parse LLM output as JSON — using fallback nodes")
        return _fallback_nodes(signals)

    # ------------------------------------------------------------------
    # Rule-based fallback
    # ------------------------------------------------------------------

    def _fallback_result(self, signals: list[dict[str, Any]]) -> DistillResult:
        return DistillResult(
            nodes=_fallback_nodes(signals),
            raw_llm_output="",
            used_fallback=True,
            model_used="rule-based-fallback",
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _read_preview(path: str, max_chars: int) -> str:
    """Read the first `max_chars` characters of a file, silently on error."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return text[:max_chars]
    except OSError:
        return ""


def _normalise_node(
    raw: dict[str, Any], signals: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Ensure every node dict has the required fields.
    Fills in defaults from the signals when the LLM omits them.
    """
    file_refs = [s.get("file_path", "") for s in signals if s.get("file_path")]
    type_meta = raw.get("type_metadata") or {}

    return {
        "summary": raw.get("summary", "File changed — intent not determined"),
        "rationale": raw.get(
            "rationale", "Rationale not explicitly stated in session signals."
        ),
        "area": raw.get("area", "general"),
        "alternatives": raw.get("alternatives") or [],
        "dependencies": raw.get("dependencies") or [],
        "confidence": float(raw.get("confidence", 0.5)),
        "importance": 0.5,
        "status": "pending",
        "type_metadata": {
            "file_refs": type_meta.get("file_refs") or file_refs,
            "packages": type_meta.get("packages") or [],
        },
    }


def _fallback_nodes(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Generate minimal rule-based node dicts when no LLM is available.

    Groups signals by their type and emits one node per unique (area, file)
    combination so the Librarian can at least record that something changed.
    """
    nodes: list[dict[str, Any]] = []
    for sig in signals:
        path = sig.get("file_path", "unknown")
        sig_type = sig.get("signal_type", "file_modify")
        ext = Path(path).suffix.lower()

        # Map extension + signal type to an area
        area = _infer_area(path, ext)
        summary = f"{sig_type.replace('_', ' ').title()}: {Path(path).name}"

        nodes.append({
            "summary": summary,
            "rationale": "Rationale not explicitly stated in session signals.",
            "area": area,
            "alternatives": [],
            "dependencies": [],
            "confidence": 0.4,  # Rule-based nodes get a medium-low confidence
            "importance": 0.5,
            "status": "pending",
            "type_metadata": {
                "file_refs": [path],
                "packages": [],
            },
        })

    return nodes


def _infer_area(path: str, ext: str) -> str:
    """Heuristic: map file path patterns to an architectural area name."""
    lower = path.lower()
    if "test" in lower or "spec" in lower:
        return "testing"
    if "config" in lower or ext in {".env", ".ini", ".toml", ".cfg"}:
        return "configuration"
    if ext == ".json" and "package" in lower:
        return "dependencies"
    if ext in {".md", ".rst", ".txt"}:
        return "documentation"
    if "schema" in lower or "migration" in lower:
        return "database"
    if "auth" in lower or "login" in lower or "token" in lower:
        return "authentication"
    if "agent" in lower:
        return "agent-architecture"
    if "server" in lower or "api" in lower or "route" in lower:
        return "api-design"
    if ext == ".py":
        return "implementation"
    return "general"
