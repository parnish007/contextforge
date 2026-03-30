"""
ContextForge v3.0 — HITL Gate Skill

Human-in-the-Loop approval gate for decision nodes.

Spec reference: OMEGA_SPEC.md §3.3 step 5 — HITL Confidence Thresholds:
  ≥ 0.85  → auto-approve  (status = "active")
  0.50–0.84 → HITL review  (pause, ask user)
  < 0.50  → low-confidence HITL (pause with extra warning)

This module implements a CLI-based HITL gate.  When the dashboard (Phase 3)
is available, the same interface can route approvals through the dashboard
approval queue instead.

The gate is invoked by GhostCoder after distillation and before the Librarian
write.  It returns one of three decisions:
  "approved"  → node is written as-is (status set to "active")
  "edited"    → node is written with user-supplied summary (status "active")
  "rejected"  → node is discarded (Librarian never called)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal

from loguru import logger

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False


# ---------------------------------------------------------------------------
# Constants (overridable via env)
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD = float(os.getenv("HITL_CONFIDENCE_THRESHOLD", "0.70"))
_AUTO_APPROVE = os.getenv("HITL_AUTO_APPROVE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

Decision = Literal["approved", "edited", "rejected"]

@dataclass
class HITLResult:
    decision: Decision
    node: dict          # Original or user-edited node dict
    edited_summary: str | None = None


# ---------------------------------------------------------------------------
# HITLGate
# ---------------------------------------------------------------------------

class HITLGate:
    """
    CLI-based Human-in-the-Loop approval gate.

    Parameters
    ----------
    confidence_threshold : float
        Nodes with confidence < this value are routed to human review.
        Nodes ≥ this value are auto-approved.
        Defaults to HITL_CONFIDENCE_THRESHOLD env var (0.70).
    auto_approve : bool
        If True, bypass all human prompts and approve everything.
        Useful for batch testing or CI.  Defaults to HITL_AUTO_APPROVE env var.
    """

    def __init__(
        self,
        confidence_threshold: float = _DEFAULT_THRESHOLD,
        auto_approve: bool = _AUTO_APPROVE,
    ):
        self.confidence_threshold = confidence_threshold
        self.auto_approve = auto_approve
        self._console = Console(stderr=False) if _RICH else None

    def review(self, node: dict) -> HITLResult:
        """
        Gate a single DecisionNode candidate.

        Returns a HITLResult with the decision and the (possibly edited) node.
        """
        confidence = float(node.get("confidence", 0.5))
        summary = node.get("summary", "")
        area = node.get("area", "general")

        # ── Auto-approve path ─────────────────────────────────────────
        if self.auto_approve:
            logger.debug(f"HITL: auto-approve enabled — passing node [{area}]")
            return HITLResult(decision="approved", node={**node, "status": "active"})

        if confidence >= self.confidence_threshold:
            logger.info(
                f"HITL: confidence {confidence:.2f} ≥ threshold "
                f"{self.confidence_threshold:.2f} — auto-approved [{area}]"
            )
            return HITLResult(decision="approved", node={**node, "status": "active"})

        # ── Human review path ─────────────────────────────────────────
        return self._prompt_user(node, confidence, summary, area)

    def review_batch(self, nodes: list[dict]) -> list[HITLResult]:
        """Gate a list of nodes. Returns one HITLResult per node."""
        results = []
        for i, node in enumerate(nodes):
            logger.info(
                f"HITL: reviewing node {i+1}/{len(nodes)} "
                f"confidence={node.get('confidence', 0):.2f}"
            )
            results.append(self.review(node))
        return results

    # ------------------------------------------------------------------
    # Prompt logic
    # ------------------------------------------------------------------

    def _prompt_user(
        self, node: dict, confidence: float, summary: str, area: str
    ) -> HITLResult:
        """Present the node to the user and collect a decision."""
        if _RICH and self._console:
            return self._rich_prompt(node, confidence, summary, area)
        return self._plain_prompt(node, confidence, summary, area)

    def _rich_prompt(
        self, node: dict, confidence: float, summary: str, area: str
    ) -> HITLResult:
        """Rich-formatted terminal prompt."""
        c = self._console
        assert c is not None

        # Colour-code confidence level
        conf_color = "yellow" if confidence >= 0.50 else "red"
        conf_str = f"[{conf_color}]{confidence:.2f}[/{conf_color}]"
        warn = (
            "[red bold]⚠  LOW CONFIDENCE[/red bold]"
            if confidence < 0.50
            else "[yellow]⚡ REVIEW REQUIRED[/yellow]"
        )

        panel_content = (
            f"{warn}\n\n"
            f"[bold]Intent :[/bold]  {summary}\n"
            f"[bold]Area   :[/bold]  {area}\n"
            f"[bold]Conf.  :[/bold]  {conf_str}  (threshold: {self.confidence_threshold:.2f})\n"
            f"[bold]Agent  :[/bold]  {node.get('created_by_agent', 'GhostCoder')}\n"
            f"[bold]File(s):[/bold]  "
            + ", ".join(
                node.get("type_metadata", {}).get("file_refs", [])[:3]
            )
        )

        c.print()
        c.print(
            Panel(
                panel_content,
                title="[bold cyan]Ghost-Coder — HITL Review[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

        choice = Prompt.ask(
            "  Approve? ",
            choices=["y", "n", "e"],
            default="y",
            show_choices=True,
            show_default=True,
        ).lower().strip()

        return self._handle_choice(choice, node, c)

    def _plain_prompt(
        self, node: dict, confidence: float, summary: str, area: str
    ) -> HITLResult:
        """Plain-text fallback prompt (no rich)."""
        warn = "⚠ LOW CONFIDENCE" if confidence < 0.50 else "⚡ REVIEW REQUIRED"
        print(f"\n{'─'*60}")
        print(f"  Ghost-Coder — HITL Review  {warn}")
        print(f"  Intent : {summary}")
        print(f"  Area   : {area}")
        print(f"  Conf.  : {confidence:.2f}  (threshold: {self.confidence_threshold:.2f})")
        print(f"{'─'*60}")
        choice = input("  Approve? (y/n/e) [y]: ").strip().lower() or "y"
        return self._handle_choice(choice, node, None)

    def _handle_choice(
        self, choice: str, node: dict, console
    ) -> HITLResult:
        if choice == "n":
            logger.info(f"HITL: node rejected by user — [{node.get('area')}] {node.get('summary','')[:60]}")
            if console:
                console.print("  [red]✗ Rejected — node will not be persisted.[/red]\n")
            return HITLResult(decision="rejected", node=node)

        if choice == "e":
            # User wants to edit the summary
            if console:
                new_summary = Prompt.ask(
                    "  New summary",
                    default=node.get("summary", ""),
                    console=console,
                )
            else:
                current = node.get("summary", "")
                new_summary = input(f"  New summary [{current}]: ").strip() or current

            edited = {**node, "summary": new_summary, "status": "active"}
            logger.info(f"HITL: node edited — [{node.get('area')}] '{new_summary[:60]}'")
            if console:
                console.print("  [green]✓ Edited and approved.[/green]\n")
            return HITLResult(decision="edited", node=edited, edited_summary=new_summary)

        # Default: y / approved
        logger.info(f"HITL: node approved — [{node.get('area')}] {node.get('summary','')[:60]}")
        if console:
            console.print("  [green]✓ Approved.[/green]\n")
        return HITLResult(decision="approved", node={**node, "status": "active"})
