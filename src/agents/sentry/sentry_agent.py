"""
ContextForge v3.0 — Agent 1: Sentry (The Watcher)

Monitors the project file system for changes to .py, .md, and .json files.
No LLM required — pure deterministic event-driven logic.

Responsibilities:
  1. Watch file system via watchdog
  2. Debounce rapid save events (2-second window, essential on Windows)
  3. Deduplicate via SHA-256 content hashing (ignore identical re-saves)
  4. Batch signals (default threshold: 3) or flush on pause (default: 30 s)
  5. Broadcast a SignalBatch message to the AgentScope MsgHub
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from agentscope.agent import AgentBase
from agentscope.message import Msg
from loguru import logger
from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from src.core.signals import (
    ContextSignal,
    SignalBatch,
    SignalType,
)

# File extensions the Sentry watches
WATCHED_EXTENSIONS: frozenset[str] = frozenset({".py", ".md", ".json"})

# Seconds to wait after the last event before treating it as a distinct save
DEBOUNCE_SECONDS: float = 2.0


def _sha256(path: str) -> str | None:
    """Return the SHA-256 hex digest of a file's content, or None on error."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _signal_type_for_event(event: FileSystemEvent) -> SignalType:
    """Map a watchdog event to the closest SignalType."""
    ext = Path(event.src_path).suffix.lower()

    if isinstance(event, FileCreatedEvent):
        return SignalType.FILE_CREATE

    if isinstance(event, FileModifiedEvent):
        if ext == ".json" and (
            "package" in event.src_path or "requirements" in event.src_path
        ):
            return SignalType.PACKAGE_INSTALL
        if ext == ".json" and "schema" in event.src_path:
            return SignalType.SCHEMA_CHANGE
        if ext in {".json", ".toml", ".ini", ".env", ".cfg"}:
            return SignalType.CONFIG_CHANGE
        return SignalType.FILE_MODIFY

    return SignalType.FILE_MODIFY


class _DebounceHandler(FileSystemEventHandler):
    """
    Watchdog event handler with debounce + SHA-256 deduplication.

    On every qualifying filesystem event it:
      1. Cancels any pending debounce timer for that path.
      2. Starts a new DEBOUNCE_SECONDS timer.
      3. When the timer fires, computes the file hash and, if it differs from
         the last seen hash, calls `on_signal` with a ContextSignal.
    """

    def __init__(
        self,
        on_signal: Callable[[ContextSignal], None],
        project_id: str | None = None,
    ):
        super().__init__()
        self._on_signal = on_signal
        self._project_id = project_id
        self._timers: dict[str, threading.Timer] = {}
        self._last_hashes: dict[str, str] = {}
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._schedule(event)

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._schedule(event)

    def _schedule(self, event: FileSystemEvent) -> None:
        path = event.src_path
        if Path(path).suffix.lower() not in WATCHED_EXTENSIONS:
            return

        with self._lock:
            # Cancel existing debounce timer for this path
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()

            # Schedule a new one
            timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self._fire,
                args=(path, event),
            )
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str, event: FileSystemEvent) -> None:
        """Called after debounce window — deduplicate then emit signal."""
        with self._lock:
            self._timers.pop(path, None)

        content_hash = _sha256(path)
        if content_hash is None:
            return  # File vanished between event and hash computation

        with self._lock:
            if self._last_hashes.get(path) == content_hash:
                logger.debug(f"Sentry: skipped duplicate save for {path}")
                return
            self._last_hashes[path] = content_hash

        signal = ContextSignal(
            signal_type=_signal_type_for_event(event),
            file_path=path,
            content_hash=content_hash,
            project_id=self._project_id,
            metadata={"event_class": type(event).__name__},
        )
        logger.debug(f"Sentry: signal emitted — {signal.signal_type} @ {path}")
        self._on_signal(signal)


class SentryAgent(AgentBase):
    """
    Agent 1 — The Watcher.

    Monitors the file system for .py / .md / .json changes and buffers them
    into SignalBatches that are broadcast to the AgentScope MsgHub.

    Parameters
    ----------
    name : str
        AgentScope agent name.
    watch_path : str
        Root directory to monitor (defaults to CWD).
    batch_threshold : int
        Number of signals that triggers an immediate batch flush.
    pause_timeout : int
        Seconds of inactivity after which the buffer is flushed.
    project_id : str | None
        Attached to every ContextSignal for downstream routing.
    """

    def __init__(
        self,
        name: str = "Sentry",
        watch_path: str = ".",
        batch_threshold: int = 3,
        pause_timeout: int = 30,
        project_id: str | None = None,
    ):
        super().__init__()
        self.name = name
        self.watch_path = os.path.abspath(watch_path)
        self.batch_threshold = batch_threshold
        self.pause_timeout = pause_timeout
        self.project_id = project_id

        self._signal_buffer: list[ContextSignal] = []
        self._buffer_lock = threading.Lock()
        self._last_activity: datetime = datetime.utcnow()

        self._observer: Observer | None = None
        self._flush_task: asyncio.Task | None = None

        logger.info(
            f"Sentry initialised — watching: {self.watch_path} "
            f"(threshold={batch_threshold}, pause={pause_timeout}s)"
        )

    # ------------------------------------------------------------------
    # AgentScope AgentBase interface
    # ------------------------------------------------------------------

    async def reply(self, x: Msg | None = None) -> Msg:
        """
        AgentScope hook for handling inbound messages.

        When invoked with a file-system event dict in x.content, the Sentry
        classifies it, buffers it, and returns either a batch-capture Msg or
        a noop Msg.
        """
        if x is not None and x.metadata and isinstance(x.metadata, dict):
            self._handle_event_dict(x.metadata)

        batch = self._try_flush(trigger="msghub")
        if batch:
            p = {"action": "batch_capture", "batch": batch.model_dump()}
            return Msg(self.name, content="batch_capture", role="assistant", metadata=p)
        return Msg(self.name, content="noop", role="assistant", metadata={"action": "noop"})

    # ------------------------------------------------------------------
    # Watchdog lifecycle
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """Start the watchdog observer in a background thread."""
        if self._observer and self._observer.is_alive():
            logger.warning("Sentry: observer already running")
            return

        handler = _DebounceHandler(
            on_signal=self._on_signal,
            project_id=self.project_id,
        )
        self._observer = Observer()
        self._observer.schedule(handler, self.watch_path, recursive=True)
        self._observer.start()
        logger.info(f"Sentry: watchdog observer started on {self.watch_path}")

    def stop_watching(self) -> None:
        """Stop the watchdog observer cleanly."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("Sentry: watchdog observer stopped")

    # ------------------------------------------------------------------
    # Signal handling and batching
    # ------------------------------------------------------------------

    def _on_signal(self, signal: ContextSignal) -> None:
        """Receive a debounced, deduplicated signal from the handler."""
        with self._buffer_lock:
            self._signal_buffer.append(signal)
            self._last_activity = datetime.utcnow()
            buffer_len = len(self._signal_buffer)

        logger.info(
            f"Sentry: buffered signal [{buffer_len}/{self.batch_threshold}] "
            f"{signal.signal_type} — {signal.file_path}"
        )

        if buffer_len >= self.batch_threshold:
            batch = self._try_flush(trigger="threshold")
            if batch:
                self._broadcast(batch)

    def _handle_event_dict(self, event: dict) -> None:
        """Handle a raw event dict arriving via AgentScope MsgHub."""
        try:
            signal = ContextSignal(
                signal_type=SignalType(event.get("type", "file_modify")),
                file_path=event.get("file_path", ""),
                content_hash=event.get("content_hash", ""),
                project_id=self.project_id,
                metadata=event.get("metadata", {}),
            )
            if signal.is_decision_signal():
                self._on_signal(signal)
        except (ValueError, KeyError) as exc:
            logger.warning(f"Sentry: malformed event dict — {exc}")

    def _try_flush(self, trigger: str = "threshold") -> SignalBatch | None:
        """
        If the buffer has any signals AND the threshold or pause condition is
        met, drain the buffer and return a SignalBatch. Otherwise return None.
        """
        with self._buffer_lock:
            if not self._signal_buffer:
                return None

            threshold_met = len(self._signal_buffer) >= self.batch_threshold
            elapsed = (datetime.utcnow() - self._last_activity).total_seconds()
            pause_met = elapsed >= self.pause_timeout

            if not (threshold_met or pause_met or trigger == "msghub"):
                return None

            batch_signals = self._signal_buffer.copy()
            self._signal_buffer.clear()

        flush_trigger = (
            "threshold" if threshold_met else "pause" if pause_met else trigger
        )
        batch = SignalBatch(
            signals=batch_signals,
            batch_id=str(uuid.uuid4()),
            triggered_by=flush_trigger,
        )
        logger.info(
            f"Sentry: flushing batch of {len(batch_signals)} signal(s) "
            f"[trigger={flush_trigger}]"
        )
        return batch

    def _broadcast(self, batch: SignalBatch) -> None:
        """
        Publish the batch to the AgentScope MsgHub.

        The MsgHub forwards the message to Token-Gater, which decides whether
        to route to Ghost-Coder directly or escalate to Architect.
        """
        p = {"action": "batch_capture", "batch": batch.model_dump()}
        msg = Msg(self.name, content="batch_capture", role="assistant", metadata=p)
        logger.info(
            f"Sentry → MsgHub: broadcasting batch {batch.batch_id} "
            f"({len(batch.signals)} signals)"
        )
        return msg
