"""
ContextForge v3.0 — Core Signal Definitions
Typed data contracts emitted by the Sentry and consumed by the MsgHub pipeline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    """Categories of file-system events that indicate a potential decision."""

    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    CONFIG_CHANGE = "config_change"
    PACKAGE_INSTALL = "package_install"
    SCHEMA_CHANGE = "schema_change"
    ARCHITECTURE_DECISION = "architecture_decision"
    LIBRARY_SELECTION = "library_selection"
    API_DESIGN = "api_design"
    METHODOLOGY_CHOICE = "methodology_choice"


# Signal types that are considered decision signals worth capturing
DECISION_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.FILE_CREATE,
        SignalType.FILE_MODIFY,
        SignalType.CONFIG_CHANGE,
        SignalType.PACKAGE_INSTALL,
        SignalType.SCHEMA_CHANGE,
        SignalType.ARCHITECTURE_DECISION,
        SignalType.LIBRARY_SELECTION,
        SignalType.API_DESIGN,
        SignalType.METHODOLOGY_CHOICE,
    }
)


class ContextSignal(BaseModel):
    """
    A single raw signal emitted by the Sentry agent.

    Signals are accumulated in the Sentry's buffer until the batch threshold
    is reached or a pause is detected, then forwarded as a batch to the
    MsgHub pipeline (Token-Gater → Ghost-Coder → Shadow-Reviewer …).
    """

    signal_type: SignalType
    file_path: str
    content_hash: str  # SHA-256 of file content at capture time — for deduplication
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optional project context attached by Sentry if PROJECT_ID is set
    project_id: str | None = None

    class Config:
        use_enum_values = True

    def is_decision_signal(self) -> bool:
        """Return True when this signal warrants pipeline processing."""
        return SignalType(self.signal_type) in DECISION_SIGNAL_TYPES


class SignalBatch(BaseModel):
    """
    A batch of ContextSignals ready for the Ghost-Coder pipeline.

    Sentry emits a SignalBatch when either:
      - signal buffer reaches batch_threshold (default 3), OR
      - pause_timeout seconds elapse with no new activity (default 30 s).
    """

    signals: list[ContextSignal]
    batch_id: str  # UUID generated at flush time
    flushed_at: datetime = Field(default_factory=datetime.utcnow)
    triggered_by: str = "threshold"  # "threshold" | "pause" | "manual"
