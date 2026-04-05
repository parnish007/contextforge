"""
ContextForge Nexus — Universal Hub Integration Tests
====================================================

Verifies that the Nexus can successfully feed structured context to an
external mock agent, and that the Permission-Gated filter correctly
enforces access-control policies per registered agent.

Test groups
───────────
  Group 1 — MemoryBus pub/sub (5 tests)
  Group 2 — HubConnector query / entity / history (8 tests)
  Group 3 — Permission-Gated filter (10 tests)
    3a  System-protected events always blocked
    3b  allow-list enforcement
    3c  deny-list enforcement
    3d  Job-Agent policy (USER_INPUT, RESEARCH, NODE_APPROVED, TASK_DONE only)
  Group 4 — ExternalAgentAdapter dispatch (5 tests)
  Group 5 — Context feed round-trip with mock agent (7 tests)

Usage
─────
  python -m pytest tests/test_hub_integration.py -v
  python -X utf8 tests/test_hub_integration.py        # standalone runner
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bridge.hub_connector import (
    HubConnector,
    MemoryBus,
    PermissionPolicy,
    _SYSTEM_PROTECTED_TYPES,
    get_hub,
)
from src.memory.ledger import EventType


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_hub(tmp_path: Path | None = None) -> HubConnector:
    """Create a HubConnector wired to a temp database, no HTTP server."""
    db = str((tmp_path or Path(tempfile.mkdtemp())) / "test.db")
    hub = HubConnector(
        db_path      = db,
        project_root = str(Path(__file__).resolve().parents[1]),
        port         = 9099,   # never started
    )
    return hub


def _make_policy(allowed: list[str] | None = None, blocked: list[str] | None = None) -> PermissionPolicy:
    return PermissionPolicy(
        allowed_event_types = allowed or [],
        blocked_event_types = blocked or [],
    )


# ── Group 1 — MemoryBus ───────────────────────────────────────────────────────

class TestMemoryBus:
    def test_subscribe_and_receive_all(self):
        """Subscriber with no filter receives every event."""
        bus    = MemoryBus()
        events = []
        bus.subscribe(lambda et, c: events.append((et, c)))
        bus.publish(EventType.NODE_APPROVED, {"summary": "test"})
        bus.publish(EventType.USER_INPUT,    {"text": "hello"})
        assert len(events) == 2

    def test_subscribe_filtered(self):
        """Subscriber with filter only receives matching event types."""
        bus    = MemoryBus()
        got    = []
        bus.subscribe(lambda et, c: got.append(et), filter_types=[EventType.NODE_APPROVED])
        bus.publish(EventType.NODE_APPROVED, {})
        bus.publish(EventType.USER_INPUT,    {})
        assert got == ["node_approved"]

    def test_multiple_subscribers(self):
        """Multiple subscribers all receive the same event."""
        bus  = MemoryBus()
        a, b = [], []
        bus.subscribe(lambda et, c: a.append(et))
        bus.subscribe(lambda et, c: b.append(et))
        bus.publish(EventType.RESEARCH, {"topic": "entropy"})
        assert a and b

    def test_unsubscribe_all(self):
        """After unsubscribe_all no callbacks are invoked."""
        bus  = MemoryBus()
        got  = []
        bus.subscribe(lambda et, c: got.append(et))
        bus.unsubscribe_all()
        bus.publish(EventType.NODE_APPROVED, {})
        assert got == []

    def test_subscriber_exception_is_isolated(self):
        """A crashing subscriber does not prevent other subscribers from running."""
        bus = MemoryBus()
        got = []
        bus.subscribe(lambda et, c: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe(lambda et, c: got.append(et))
        bus.publish(EventType.TASK_DONE, {})
        assert got == ["task_done"]


# ── Group 2 — Core query methods ─────────────────────────────────────────────

class TestHubQuery:
    @pytest.fixture
    def hub(self, tmp_path):
        h = _make_hub(tmp_path)
        # Stub out subsystems so no real DB/index calls are needed
        h._ledger.list_events = MagicMock(return_value=[
            {
                "event_type": "user_input",
                "content":    {"text": "user wants JWT service", "agent": "sentry"},
                "created_at": "2026-04-01T00:00:00Z",
            },
            {
                "event_type": "node_approved",
                "content":    {"summary": "JWT implemented", "agent": "coder"},
                "created_at": "2026-04-01T00:01:00Z",
            },
            {
                "event_type": "conflict",
                "content":    {"detail": "charter violation", "agent": "reviewer"},
                "created_at": "2026-04-01T00:02:00Z",
            },
        ])
        h._storage.search_nodes = MagicMock(return_value=[])
        h._indexer.search       = MagicMock(return_value=[])
        return h

    def test_query_returns_response(self, hub):
        result = asyncio.get_event_loop().run_until_complete(
            hub.query("JWT service")
        )
        assert result.query == "JWT service"
        assert result.total >= 0

    def test_query_excludes_system_protected_by_default(self, hub):
        """With no agent_id, CONFLICT events must not appear in results."""
        result = asyncio.get_event_loop().run_until_complete(
            hub.query("charter violation")
        )
        types = {r.event_type for r in result.results}
        assert "conflict" not in types

    def test_query_sources_ledger_only(self, hub):
        result = asyncio.get_event_loop().run_until_complete(
            hub.query("JWT", sources=["ledger"])
        )
        for r in result.results:
            assert r.source == "ledger"

    def test_get_history_excludes_system_protected(self, hub):
        events = asyncio.get_event_loop().run_until_complete(hub.get_history(n=10))
        types = {e.get("event_type") for e in events}
        assert "conflict" not in types

    def test_get_history_agent_filter(self, hub):
        events = asyncio.get_event_loop().run_until_complete(
            hub.get_history(n=10, agent="coder")
        )
        for e in events:
            assert e.get("content", {}).get("agent") == "coder"

    def test_export_memory_structure(self, hub):
        hub._ledger.list_events      = MagicMock(return_value=[])
        hub._storage.search_nodes    = MagicMock(return_value=[])
        hub._ledger.reconstruct_state = MagicMock(return_value="<state>")
        snap = asyncio.get_event_loop().run_until_complete(hub.export_memory())
        assert "exported_at" in snap
        assert "events" in snap
        assert "nodes" in snap

    def test_elapsed_ms_positive(self, hub):
        result = asyncio.get_event_loop().run_until_complete(hub.query("test"))
        assert result.elapsed_ms >= 0

    def test_to_dict_serialisable(self, hub):
        result = asyncio.get_event_loop().run_until_complete(hub.query("test"))
        raw    = json.dumps(result.to_dict())   # must not raise
        assert "query" in raw


# ── Group 3 — Permission-Gated filter ────────────────────────────────────────

class TestPermissionGate:
    """Exhaustive coverage of PermissionPolicy and _filter_results."""

    # 3a — System-protected types always blocked ──────────────────────────

    def test_conflict_always_blocked_no_policy(self):
        hub = _make_hub()
        from src.bridge.hub_connector import ContextResult
        results = [
            ContextResult(source="ledger", event_type="conflict",
                          content={}, score=0.9),
            ContextResult(source="ledger", event_type="user_input",
                          content={}, score=0.8),
        ]
        filtered = hub._filter_results(results, agent_id=None)
        types    = {r.event_type for r in filtered}
        assert "conflict"   not in types
        assert "user_input" in    types

    def test_rollback_always_blocked_no_policy(self):
        hub = _make_hub()
        from src.bridge.hub_connector import ContextResult
        results = [ContextResult(source="ledger", event_type="rollback", content={}, score=0.5)]
        assert hub._filter_results(results, None) == []

    def test_checkpoint_always_blocked_no_policy(self):
        hub = _make_hub()
        from src.bridge.hub_connector import ContextResult
        results = [ContextResult(source="ledger", event_type="checkpoint", content={}, score=0.5)]
        assert hub._filter_results(results, None) == []

    def test_system_protected_cannot_be_unlocked_via_allow_list(self):
        """Even if an agent has 'conflict' in allowed_event_types it stays blocked."""
        policy = _make_policy(allowed=["conflict", "user_input"])
        assert policy.is_permitted("conflict")  is False
        assert policy.is_permitted("user_input") is True

    # 3b — allow-list enforcement ─────────────────────────────────────────

    def test_allow_list_restricts_other_types(self):
        policy = _make_policy(allowed=["user_input", "research"])
        assert policy.is_permitted("user_input")   is True
        assert policy.is_permitted("research")     is True
        assert policy.is_permitted("agent_thought") is False
        assert policy.is_permitted("file_diff")    is False

    def test_empty_allow_list_permits_all_non_protected(self):
        policy = _make_policy(allowed=[])
        assert policy.is_permitted("user_input")  is True
        assert policy.is_permitted("node_approved") is True
        assert policy.is_permitted("conflict")    is False   # still protected

    # 3c — deny-list enforcement ──────────────────────────────────────────

    def test_blocked_list_applied(self):
        policy = _make_policy(blocked=["agent_thought", "file_diff"])
        assert policy.is_permitted("agent_thought") is False
        assert policy.is_permitted("file_diff")     is False
        assert policy.is_permitted("user_input")    is True

    def test_blocked_overrides_allowed(self):
        """A type in both allow and block lists is denied (block wins)."""
        policy = _make_policy(allowed=["user_input"], blocked=["user_input"])
        assert policy.is_permitted("user_input") is False

    # 3d — Job-Agent scenario ─────────────────────────────────────────────

    def test_job_agent_policy_full_scenario(self):
        """
        Job Agent should only see USER_INPUT, RESEARCH, NODE_APPROVED, TASK_DONE.
        AGENT_THOUGHT, FILE_DIFF, CONFLICT, ROLLBACK, CHECKPOINT must be hidden.
        """
        hub = _make_hub()
        hub.register_agent("job_agent", "Handles user career data", tags=["profile"])
        hub.set_agent_permissions(
            "job_agent",
            allowed_event_types = ["user_input", "research", "node_approved", "task_done"],
            blocked_event_types = ["agent_thought", "file_diff"],
        )

        from src.bridge.hub_connector import ContextResult

        def _make(et: str) -> ContextResult:
            return ContextResult(source="ledger", event_type=et, content={}, score=0.5)

        allowed   = ["user_input", "research", "node_approved", "task_done"]
        forbidden = ["agent_thought", "file_diff", "conflict", "rollback", "checkpoint"]

        all_results = [_make(et) for et in allowed + forbidden]
        filtered    = hub._filter_results(all_results, "job_agent")
        result_types = {r.event_type for r in filtered}

        for et in allowed:
            assert et in result_types, f"{et} should be visible to job_agent"
        for et in forbidden:
            assert et not in result_types, f"{et} should be hidden from job_agent"

    def test_unregistered_agent_id_uses_default_protection(self):
        """An unknown agent_id falls back to system-protected-only filtering."""
        hub = _make_hub()
        from src.bridge.hub_connector import ContextResult
        results = [
            ContextResult(source="ledger", event_type="conflict",   content={}, score=0.9),
            ContextResult(source="ledger", event_type="user_input", content={}, score=0.8),
        ]
        filtered = hub._filter_results(results, "unknown_agent")
        types    = {r.event_type for r in filtered}
        assert "conflict"   not in types
        assert "user_input" in    types


# ── Group 4 — ExternalAgentAdapter dispatch ───────────────────────────────────

class TestAgentDispatch:
    @pytest.fixture
    def hub(self, tmp_path):
        h = _make_hub(tmp_path)
        h._ledger.list_events     = MagicMock(return_value=[])
        h._storage.search_nodes   = MagicMock(return_value=[])
        h._indexer.search         = MagicMock(return_value=[])
        return h

    def test_register_and_list(self, hub):
        hub.register_agent("agent_a", "Test agent", tags=["python", "coding"])
        assert "agent_a" in hub._agents

    def test_unregister(self, hub):
        hub.register_agent("temp_agent", "Temp", tags=[])
        hub.unregister_agent("temp_agent")
        assert "temp_agent" not in hub._agents

    def test_dispatch_to_named_agent(self, hub):
        responses = []
        async def mock_fn(ctx: str) -> str:
            responses.append(ctx)
            return "mock_response"

        hub.register_agent("scene_sorter", "Sorts scenes", tags=["scene"], query_fn=mock_fn)
        result = asyncio.get_event_loop().run_until_complete(
            hub.dispatch("sort scenes by topic", agent_id="scene_sorter")
        )
        assert result["routed_to"] == "scene_sorter"
        assert result["agent_response"] == "mock_response"
        assert "CONTEXT:" in responses[0]

    def test_dispatch_tag_routing(self, hub):
        async def fn_a(ctx): return "agent_a"
        async def fn_b(ctx): return "agent_b"

        hub.register_agent("career_agent", "Career", tags=["career", "resume"], query_fn=fn_a)
        hub.register_agent("tech_agent",   "Tech",   tags=["code", "python"],   query_fn=fn_b)

        result = asyncio.get_event_loop().run_until_complete(
            hub.dispatch("update my career resume")
        )
        assert result["routed_to"] == "career_agent"

    def test_dispatch_no_agents_returns_empty_response(self, hub):
        result = asyncio.get_event_loop().run_until_complete(
            hub.dispatch("random query")
        )
        assert result["agent_response"] == ""


# ── Group 5 — Context feed round-trip ────────────────────────────────────────

class TestContextFeedRoundTrip:
    """
    Simulate the Nexus 'feeding' structured context to an external mock agent
    and verifying the full data-path integrity.
    """

    @pytest.fixture
    def hub_with_data(self, tmp_path):
        h = _make_hub(tmp_path)

        # Inject synthetic ledger data directly
        sample_events = [
            {
                "event_type": "user_input",
                "content":    {"text": "I am a software engineer", "agent": "sentry"},
                "created_at": "2026-04-01T10:00:00Z",
            },
            {
                "event_type": "research",
                "content":    {"topic": "distributed systems", "summary": "CAP theorem", "agent": "researcher"},
                "created_at": "2026-04-01T10:01:00Z",
            },
            {
                "event_type": "node_approved",
                "content":    {"summary": "async task queue implemented", "agent": "coder"},
                "created_at": "2026-04-01T10:02:00Z",
            },
        ]
        h._ledger.list_events   = MagicMock(return_value=sample_events)
        h._storage.search_nodes = MagicMock(return_value=[])
        h._indexer.search       = MagicMock(return_value=[])
        return h

    def test_query_returns_user_input_event(self, hub_with_data):
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.query("software engineer", sources=["ledger"])
        )
        types = {r.event_type for r in result.results}
        assert "user_input" in types

    def test_context_payload_json_serialisable(self, hub_with_data):
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.query("distributed systems", sources=["ledger"])
        )
        raw = json.dumps(result.to_dict())
        assert "research" in raw or "node_approved" in raw

    def test_mock_agent_receives_context(self, hub_with_data):
        """Full round-trip: query feeds context into a mock agent's query_fn."""
        received = {}

        async def mock_agent_fn(ctx: str) -> str:
            received["ctx"] = ctx
            # Parse the JSON context block
            json_start = ctx.find("{")
            if json_start >= 0:
                parsed        = json.loads(ctx[json_start:ctx.rfind("}") + 1])
                received["q"] = parsed.get("query")
            return "processed"

        hub_with_data.register_agent(
            "job_agent", "Career agent", tags=["engineer", "software"],
            query_fn=mock_agent_fn,
        )
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.dispatch("software engineer background", agent_id="job_agent")
        )
        assert result["agent_response"] == "processed"
        assert "CONTEXT:" in received.get("ctx", "")

    def test_job_agent_cannot_see_conflict_events_in_feed(self, hub_with_data):
        """Even if conflict events are injected into ledger, job_agent must not receive them."""
        hub_with_data._ledger.list_events = MagicMock(return_value=[
            {"event_type": "conflict",   "content": {"detail": "violation"}, "created_at": "2026"},
            {"event_type": "user_input", "content": {"text": "hello"},        "created_at": "2026"},
        ])
        hub_with_data.register_agent("job_agent", "job", tags=[])
        hub_with_data.set_agent_permissions(
            "job_agent",
            allowed_event_types=["user_input", "research"],
        )
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.query("hello", sources=["ledger"], agent_id="job_agent")
        )
        types = {r.event_type for r in result.results}
        assert "conflict"   not in types
        assert "user_input" in    types

    def test_bus_fires_on_publish(self, hub_with_data):
        received = []
        hub_with_data.bus.subscribe(lambda et, c: received.append(et))
        hub_with_data.publish(EventType.NODE_APPROVED, {"summary": "test node"})
        assert "node_approved" in received

    def test_query_deduplicates_identical_results(self, hub_with_data):
        """Duplicate content hashes must produce a single result."""
        dup_events = [
            {"event_type": "user_input", "content": {"text": "same"}, "created_at": "2026"},
            {"event_type": "user_input", "content": {"text": "same"}, "created_at": "2026"},
        ]
        hub_with_data._ledger.list_events = MagicMock(return_value=dup_events)
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.query("same", sources=["ledger"])
        )
        # Both have identical content hash → only one should survive dedup
        assert result.total == 1

    def test_elapsed_ms_under_500ms(self, hub_with_data):
        result = asyncio.get_event_loop().run_until_complete(
            hub_with_data.query("test", sources=["ledger"])
        )
        # Stub calls should complete well under 500 ms
        assert result.elapsed_ms < 500.0


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import unittest

    # Collect all test classes
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestMemoryBus, TestPermissionGate, TestAgentDispatch]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # pytest-style groups that use fixtures run under pytest only
    print("\nNOTE: Groups 2 and 5 (fixture-based) require: pytest tests/test_hub_integration.py -v")
    sys.exit(0 if result.wasSuccessful() else 1)
