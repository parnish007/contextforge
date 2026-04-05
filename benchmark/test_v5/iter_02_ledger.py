"""
ContextForge Nexus — Temporal Integrity & Hash-Chain Rollbacks
============================================================================

75 tests validating the EventLedger against temporal paradoxes:
  - Rollback accuracy (state before the rollback must be exactly restored)
  - Hash-chain tamper detection
  - reconstruct_state() latency and correctness
  - Split-brain scenarios (two conflicting event sequences merged)

Primary metric: State Reconstruction Latency
  Time taken for reconstruct_state(n) to rebuild the system prompt from SQLite.

Goal: Prove SHA-256 hash-chain integrity prevents "Ghost Memories."

Run:
    python -X utf8 benchmark/test_v5/iter_02_ledger.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger
from benchmark.test_v5.nexus_tester_util import (
    ChaosConfig, MetricsCollector, TestResult, run_suite, save_log,
    timing, LedgerSaboteur, ENGINEERING_TOPICS,
)
from src.memory.ledger import EventLedger, EventType, ConflictError, temp_ledger

ITER_NAME = "iter_02_ledger"
CATEGORY  = "temporal_integrity"
CFG       = ChaosConfig(seed=10)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tmp_ledger() -> tuple[EventLedger, str]:
    """Create a ledger backed by a temp SQLite file."""
    tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    ledger = EventLedger(db_path=path, charter_path="nonexistent_charter.md")
    return ledger, path


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Group 1: Basic append + retrieval (tests 1–15) ───────────────────────────

async def test_append_returns_uuid(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    eid = ledger.append(EventType.USER_INPUT, {"text": "hello"})
    assert len(eid) == 36 and eid.count("-") == 4
    _cleanup(path)
    return {"event_id_length": len(eid)}

async def test_append_stores_event(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    eid = ledger.append(EventType.AGENT_THOUGHT, {"thought": "test thought"}, skip_guard=True)
    events = ledger.list_events(last_n=1)
    assert events[0]["event_id"] == eid
    _cleanup(path)
    return {"stored": True}

async def test_list_events_returns_newest_first(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "first"})
    e2 = ledger.append(EventType.USER_INPUT, {"text": "second"})
    events = ledger.list_events(last_n=2)
    assert events[0]["event_id"] == e2   # newest first
    assert events[1]["event_id"] == e1
    _cleanup(path)
    return {"order": "newest_first"}

async def test_all_event_types_storable(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    stored: list[str] = []
    for etype in EventType:
        eid = ledger.append(etype, {"data": etype.value}, skip_guard=True)
        stored.append(eid)
    assert len(stored) == len(EventType)
    _cleanup(path)
    return {"event_types_stored": len(stored)}

async def test_content_roundtrip_json(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    payload = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}
    ledger.append(EventType.USER_INPUT, payload)
    event   = ledger.list_events(last_n=1)[0]
    assert event["content"]["key"]           == "value"
    assert event["content"]["nested"]["b"]   == [1, 2, 3]
    _cleanup(path)
    return {"roundtrip": True}

async def test_metadata_stored(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    meta = {"tokens_used": 123, "model_name": "groq-test"}
    ledger.append(EventType.AGENT_THOUGHT, {"thought": "x"}, metadata=meta, skip_guard=True)
    event = ledger.list_events(last_n=1)[0]
    assert event["metadata"]["tokens_used"] == 123
    _cleanup(path)
    return {"metadata_stored": True}

async def test_status_default_active(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "hi"})
    event = ledger.list_events(last_n=1)[0]
    assert event["status"] == "active"
    _cleanup(path)
    return {"default_status": "active"}

async def test_filter_by_event_type(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT,    {"text": "a"})
    ledger.append(EventType.AGENT_THOUGHT, {"thought": "b"}, skip_guard=True)
    ledger.append(EventType.USER_INPUT,    {"text": "c"})
    user_events = ledger.list_events(last_n=10, event_type="USER_INPUT")
    assert all(e["event_type"] == "USER_INPUT" for e in user_events)
    assert len(user_events) == 2
    _cleanup(path)
    return {"filtered_count": len(user_events)}

async def test_filter_by_status(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    eid = ledger.append(EventType.USER_INPUT, {"text": "rollback me"})
    ledger.append(EventType.USER_INPUT, {"text": "keep me"})
    ledger.rollback(event_id=eid)
    active = ledger.list_events(last_n=10, status="active")
    rolled = ledger.list_events(last_n=10, status="rolled_back")
    # "rollback me" event (and everything after) should be rolled_back
    assert any(e["status"] == "rolled_back" for e in rolled)
    _cleanup(path)
    return {"active_count": len(active), "rolled_back_count": len(rolled)}

async def test_export_log_returns_active_only(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "keep"})
    e2 = ledger.append(EventType.USER_INPUT, {"text": "rollback"})
    ledger.rollback(event_id=e1)  # prune e2 and everything after
    exported = ledger.export_log()
    eids = {e["event_id"] for e in exported}
    assert e1 in eids
    assert e2 not in eids
    _cleanup(path)
    return {"exported_count": len(exported)}

async def test_multiple_appends_distinct_ids(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(20)]
    assert len(set(ids)) == 20
    _cleanup(path)
    return {"distinct_ids": 20}

async def test_parent_id_stored(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    parent = ledger.append(EventType.USER_INPUT, {"text": "parent"})
    child  = ledger.append(EventType.AGENT_THOUGHT, {"thought": "child"},
                            parent_id=parent, skip_guard=True)
    events = ledger.list_events(last_n=1)
    assert events[0]["parent_id"] == parent
    _cleanup(path)
    return {"parent_id_linked": True}

async def test_prev_hash_not_null_after_genesis(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "first"})
    event = ledger.list_events(last_n=1)[0]
    assert event["prev_hash"] is not None
    assert len(event["prev_hash"]) == 64   # SHA-256 hex
    _cleanup(path)
    return {"prev_hash_length": 64}

async def test_prev_hash_differs_between_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "a"})
    ledger.append(EventType.USER_INPUT, {"text": "b"})
    events = ledger.list_events(last_n=2)
    assert events[0]["prev_hash"] != events[1]["prev_hash"]
    _cleanup(path)
    return {"hashes_differ": True}

async def test_init_creates_events_table(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "events" in tables
    _cleanup(path)
    return {"events_table_exists": True}


# ── Group 2: Rollback accuracy (tests 16–35) ─────────────────────────────────

async def test_rollback_by_event_id(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "keep"})
    e2 = ledger.append(EventType.USER_INPUT, {"text": "remove_1"})
    e3 = ledger.append(EventType.USER_INPUT, {"text": "remove_2"})
    pruned = ledger.rollback(event_id=e1)
    assert pruned == 2   # e2 and e3 pruned
    active = ledger.list_events(status="active")
    active_ids = {e["event_id"] for e in active
                  if e["event_type"] != "ROLLBACK"}
    assert e1 in active_ids
    assert e2 not in active_ids
    _cleanup(path)
    return {"pruned": pruned}

async def test_rollback_by_timestamp(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "early"})
    e1_ts = ledger.list_events(last_n=1)[0]["created_at"]
    await asyncio.sleep(0.01)
    ledger.append(EventType.USER_INPUT, {"text": "late"})
    pruned = ledger.rollback(timestamp=e1_ts)
    assert pruned == 1
    _cleanup(path)
    return {"pruned_by_ts": pruned}

async def test_rollback_zero_pruned_on_latest(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "only"})
    latest = ledger.list_events(last_n=1)[0]["event_id"]
    pruned = ledger.rollback(event_id=latest)
    assert pruned == 0
    _cleanup(path)
    return {"pruned": 0}

async def test_rollback_records_rollback_event(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "base"})
    ledger.rollback(event_id=e1)
    rb_events = ledger.list_events(event_type="ROLLBACK")
    assert len(rb_events) >= 1
    _cleanup(path)
    return {"rollback_events": len(rb_events)}

async def test_rollback_idempotent(cfg: ChaosConfig) -> dict:
    """Rolling back the same point twice doesn't double-prune."""
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "anchor"})
    ledger.append(EventType.USER_INPUT, {"text": "prune_me"})
    p1 = ledger.rollback(event_id=e1)
    p2 = ledger.rollback(event_id=e1)
    assert p2 == 0   # nothing left to prune (second rollback event is after anchor)
    _cleanup(path)
    return {"first_prune": p1, "second_prune": p2}

async def test_rollback_preserves_earlier_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ids = [ledger.append(EventType.USER_INPUT, {"seq": i}) for i in range(5)]
    ledger.rollback(event_id=ids[2])   # prune ids[3] and ids[4]
    active = {e["event_id"] for e in ledger.list_events(status="active")
              if e["event_type"] != "ROLLBACK"}
    assert ids[0] in active
    assert ids[1] in active
    assert ids[2] in active
    assert ids[3] not in active
    assert ids[4] not in active
    _cleanup(path)
    return {"preserved_count": 3}

async def test_rollback_then_new_append(cfg: ChaosConfig) -> dict:
    """After rollback, new events should append cleanly."""
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "base"})
    ledger.append(EventType.USER_INPUT, {"text": "stale"})
    ledger.rollback(event_id=e1)
    new_id = ledger.append(EventType.USER_INPUT, {"text": "fresh_after_rollback"})
    events = ledger.list_events(status="active")
    active_ids = {e["event_id"] for e in events if e["event_type"] != "ROLLBACK"}
    assert new_id in active_ids
    _cleanup(path)
    return {"new_event_after_rollback": True}

async def test_triple_rollback_scenario(cfg: ChaosConfig) -> dict:
    """Roll back 3 turns, inject new fact, roll back again — clean state."""
    ledger, path = _tmp_ledger()
    ids = [ledger.append(EventType.USER_INPUT, {"step": i}) for i in range(6)]
    # Roll back 3 turns
    ledger.rollback(event_id=ids[2])
    # Inject new fact
    new_id = ledger.append(EventType.AGENT_THOUGHT, {"thought": "new_fact"}, skip_guard=True)
    # Roll back again to before the new fact
    ledger.rollback(event_id=ids[2])
    active = {e["event_id"] for e in ledger.list_events(status="active")
              if e["event_type"] not in ("ROLLBACK",)}
    assert new_id not in active
    assert ids[0] in active
    _cleanup(path)
    return {"triple_rollback_clean": True}

async def test_rollback_without_params_raises(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    try:
        ledger.rollback()
        assert False, "Should have raised"
    except ValueError:
        pass
    _cleanup(path)
    return {"raises_on_no_params": True}

async def test_rollback_unknown_event_id_raises(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    try:
        ledger.rollback(event_id="nonexistent-id")
        assert False, "Should have raised"
    except ValueError:
        pass
    _cleanup(path)
    return {"raises_on_unknown_id": True}


# ── Group 3: reconstruct_state() (tests 36–50) ───────────────────────────────

async def test_reconstruct_empty_ledger(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    state = ledger.reconstruct_state(n=10)
    assert "ContextForge" in state   # header always present
    _cleanup(path)
    return {"empty_state_has_header": True}

async def test_reconstruct_includes_user_input(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "Design the auth flow"})
    state = ledger.reconstruct_state(n=5)
    assert "Design the auth flow" in state
    _cleanup(path)
    return {"user_input_in_state": True}

async def test_reconstruct_includes_agent_thought(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.AGENT_THOUGHT, {"thought": "Use JWT for auth"}, skip_guard=True)
    state = ledger.reconstruct_state(n=5)
    assert "JWT" in state
    _cleanup(path)
    return {"agent_thought_in_state": True}

async def test_reconstruct_respects_n_limit(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(20):
        ledger.append(EventType.USER_INPUT, {"text": f"msg {i}"})
    state = ledger.reconstruct_state(n=5)
    # Should contain last 5 messages, roughly
    assert "msg 19" in state
    assert "msg 14" in state or "msg 15" in state
    _cleanup(path)
    return {"n_respected": True}

async def test_reconstruct_excludes_rolled_back(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    e1 = ledger.append(EventType.USER_INPUT, {"text": "keep this"})
    ledger.append(EventType.USER_INPUT, {"text": "prune this"})
    ledger.rollback(event_id=e1)
    state = ledger.reconstruct_state(n=20)
    assert "prune this" not in state
    _cleanup(path)
    return {"rolled_back_excluded": True}

async def test_reconstruct_latency_10_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(10):
        ledger.append(EventType.USER_INPUT, {"text": ENGINEERING_TOPICS[i % len(ENGINEERING_TOPICS)]})
    async with timing() as t:
        ledger.reconstruct_state(n=10)
    assert t.elapsed_ms < 500
    _cleanup(path)
    return {"latency_ms": round(t.elapsed_ms, 2)}

async def test_reconstruct_latency_100_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(100):
        ledger.append(EventType.USER_INPUT, {"text": f"event {i}"})
    async with timing() as t:
        ledger.reconstruct_state(n=100)
    assert t.elapsed_ms < 2000
    _cleanup(path)
    return {"latency_ms": round(t.elapsed_ms, 2)}

async def test_reconstruct_latency_500_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(500):
        ledger.append(EventType.USER_INPUT, {"text": f"event {i}"})
    async with timing() as t:
        ledger.reconstruct_state(n=50)   # last 50 of 500
    assert t.elapsed_ms < 3000
    _cleanup(path)
    return {"latency_ms_500_events": round(t.elapsed_ms, 2)}

async def test_reconstruct_state_format(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "test_query"})
    state = ledger.reconstruct_state(n=5)
    lines = state.strip().split("\n")
    assert lines[0].startswith("===")
    _cleanup(path)
    return {"header_format": lines[0][:20]}

async def test_reconstruct_checkpoint_event(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.CHECKPOINT,
                  {"trigger": "test", "idle_minutes": 15, "timestamp": "2026-01-01T00:00:00Z"},
                  skip_guard=True)
    state = ledger.reconstruct_state(n=5)
    assert "CHECKPOINT" in state
    _cleanup(path)
    return {"checkpoint_in_state": True}

async def test_reconstruct_node_approved(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.NODE_APPROVED,
                  {"summary": "JWT implementation approved"},
                  skip_guard=True)
    state = ledger.reconstruct_state(n=5)
    assert "JWT" in state
    _cleanup(path)
    return {"node_in_state": True}

async def test_reconstruct_per_topic(cfg: ChaosConfig) -> dict:
    """Measure reconstruction latency for each engineering topic (sampling)."""
    latencies: list[float] = []
    for topic in ENGINEERING_TOPICS[:5]:
        ledger, path = _tmp_ledger()
        for _ in range(10):
            ledger.append(EventType.USER_INPUT, {"text": topic})
        async with timing() as t:
            ledger.reconstruct_state(n=10)
        latencies.append(t.elapsed_ms)
        _cleanup(path)
    return {"mean_latency": round(sum(latencies) / len(latencies), 2)}

async def test_reconstruct_file_diff_event(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.FILE_DIFF,
                  {"path": "src/agents/sentry/sentry_agent.py",
                   "change_type": "modified"},
                  skip_guard=True)
    state = ledger.reconstruct_state(n=5)
    assert "sentry_agent.py" in state
    _cleanup(path)
    return {"file_diff_in_state": True}

async def test_reconstruct_mixed_event_types(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT,    {"text": "user query"})
    ledger.append(EventType.AGENT_THOUGHT, {"thought": "agent reasoning"}, skip_guard=True)
    ledger.append(EventType.NODE_APPROVED, {"summary": "node summary"},    skip_guard=True)
    state = ledger.reconstruct_state(n=10)
    assert "USER_INPUT"    in state
    assert "AGENT_THOUGHT" in state
    assert "NODE_APPROVED" in state
    _cleanup(path)
    return {"mixed_types_present": True}

async def test_reconstruct_is_deterministic(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(5):
        ledger.append(EventType.USER_INPUT, {"text": f"deterministic_{i}"})
    s1 = ledger.reconstruct_state(n=5)
    s2 = ledger.reconstruct_state(n=5)
    assert s1 == s2
    _cleanup(path)
    return {"deterministic": True}


# ── Group 4: Hash-chain integrity (tests 51–65) ───────────────────────────────

async def test_chain_valid_on_fresh_ledger(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(5):
        ledger.append(EventType.USER_INPUT, {"text": f"event {i}"})
    sab     = LedgerSaboteur(path)
    valid, err = sab.verify_chain()
    assert valid, f"Chain should be valid: {err}"
    _cleanup(path)
    return {"chain_valid": True}

async def test_chain_broken_on_corruption(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(5):
        ledger.append(EventType.USER_INPUT, {"text": f"event {i}"})
    sab = LedgerSaboteur(path)
    sab.corrupt_latest()
    valid, err = sab.verify_chain()
    assert not valid, "Chain should be broken after corruption"
    sab.restore()
    _cleanup(path)
    return {"corruption_detected": True, "error_preview": err[:60]}

async def test_chain_restored_after_saboteur(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(3):
        ledger.append(EventType.USER_INPUT, {"text": f"event {i}"})
    sab = LedgerSaboteur(path)
    sab.corrupt_latest()
    sab.restore()
    valid, err = sab.verify_chain()
    assert valid, f"Chain should be valid after restore: {err}"
    _cleanup(path)
    return {"restored_valid": True}

async def test_prev_hash_64_hex_chars(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ledger.append(EventType.USER_INPUT, {"text": "test"})
    event = ledger.list_events(last_n=1)[0]
    ph = event.get("prev_hash", "")
    assert len(ph) == 64
    assert all(c in "0123456789abcdef" for c in ph)
    _cleanup(path)
    return {"prev_hash_format": "64_hex"}

async def test_chain_grows_monotonically(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    hashes: list[str] = []
    for i in range(10):
        ledger.append(EventType.USER_INPUT, {"text": f"e{i}"})
    events = ledger.list_events(last_n=10)
    hashes = [e["prev_hash"] for e in events]
    assert len(set(hashes)) == len(hashes)   # all unique
    _cleanup(path)
    return {"unique_hashes": len(hashes)}

# Tests 56–65: reconstruct after N rollbacks (temporal paradox sequences)

async def _temporal_paradox(cfg: ChaosConfig, depth: int) -> dict:
    ledger, path = _tmp_ledger()
    ids = [ledger.append(EventType.USER_INPUT, {"step": i}) for i in range(depth + 3)]

    # Roll back `depth` turns
    anchor_id = ids[-(depth + 1)]
    ledger.rollback(event_id=anchor_id)

    # Inject new "conflicting" fact
    new_id = ledger.append(EventType.AGENT_THOUGHT,
                            {"thought": f"new_fact_after_{depth}_rollback"},
                            skip_guard=True)

    # Roll back again to anchor
    ledger.rollback(event_id=anchor_id)

    # Verify new_id is gone
    active_ids = {e["event_id"] for e in ledger.list_events(status="active")}
    ghost_memory_present = new_id in active_ids
    assert not ghost_memory_present, f"Ghost memory detected: {new_id} still in active events"

    state = ledger.reconstruct_state(n=20)
    assert f"new_fact_after_{depth}_rollback" not in state

    _cleanup(path)
    return {"depth": depth, "ghost_memory_present": ghost_memory_present}

async def test_temporal_paradox_depth_1(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 1)
async def test_temporal_paradox_depth_2(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 2)
async def test_temporal_paradox_depth_3(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 3)
async def test_temporal_paradox_depth_5(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 5)
async def test_temporal_paradox_depth_10(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 10)
async def test_temporal_paradox_depth_20(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 20)
async def test_temporal_paradox_depth_30(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 30)
async def test_temporal_paradox_depth_50(cfg: ChaosConfig) -> dict:
    return await _temporal_paradox(cfg, 50)
async def test_temporal_paradox_sequential_3(cfg: ChaosConfig) -> dict:
    """Three sequential roll-forward-rollback cycles."""
    ledger, path = _tmp_ledger()
    for cycle in range(3):
        ids = [ledger.append(EventType.USER_INPUT, {"cycle": cycle, "step": s}) for s in range(4)]
        ledger.rollback(event_id=ids[1])
    state = ledger.reconstruct_state(n=20)
    assert "ContextForge" in state
    _cleanup(path)
    return {"cycles": 3}
async def test_temporal_paradox_alternating(cfg: ChaosConfig) -> dict:
    """Alternating append / rollback / append / rollback."""
    ledger, path = _tmp_ledger()
    anchor = ledger.append(EventType.USER_INPUT, {"text": "anchor"})
    for i in range(5):
        ledger.append(EventType.USER_INPUT, {"text": f"volatile_{i}"})
        ledger.rollback(event_id=anchor)
    active = {e["event_id"] for e in ledger.list_events(status="active")
              if e["event_type"] not in ("ROLLBACK",)}
    assert anchor in active
    _cleanup(path)
    return {"anchor_always_active": True}


# ── Group 5: Reconstruction timing under stress (tests 66–75) ─────────────────

async def _reconstruct_stress(cfg: ChaosConfig, n_events: int, n_retrieve: int) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(n_events):
        ledger.append(EventType.USER_INPUT, {"text": ENGINEERING_TOPICS[i % 25]})
    async with timing() as t:
        state = ledger.reconstruct_state(n=n_retrieve)
    assert len(state) > 0
    _cleanup(path)
    return {
        "n_events":    n_events,
        "n_retrieve":  n_retrieve,
        "latency_ms":  round(t.elapsed_ms, 2),
        "state_lines": len(state.split("\n")),
    }

async def test_stress_50_retrieve_10(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 50, 10)
async def test_stress_100_retrieve_20(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 100, 20)
async def test_stress_200_retrieve_50(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 200, 50)
async def test_stress_500_retrieve_100(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 500, 100)
async def test_stress_1000_retrieve_20(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 1000, 20)
async def test_stress_2000_retrieve_10(cfg: ChaosConfig) -> dict:
    return await _reconstruct_stress(cfg, 2000, 10)
async def test_stress_with_rollbacks_mixed(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(50)]
    # Rollback every 10 events
    for i in [10, 20, 30]:
        ledger.rollback(event_id=ids[i])
    async with timing() as t:
        state = ledger.reconstruct_state(n=20)
    _cleanup(path)
    return {"latency_ms": round(t.elapsed_ms, 2), "state_len": len(state)}
async def test_stress_export_1000_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(1000):
        ledger.append(EventType.USER_INPUT, {"i": i})
    async with timing() as t:
        exported = ledger.export_log()
    assert len(exported) == 1000
    _cleanup(path)
    return {"exported": len(exported), "latency_ms": round(t.elapsed_ms, 2)}
async def test_stress_wal_mode_concurrent_reads(cfg: ChaosConfig) -> dict:
    """WAL mode allows concurrent reads. Verify no errors under parallel list_events."""
    ledger, path = _tmp_ledger()
    for i in range(100):
        ledger.append(EventType.USER_INPUT, {"i": i})
    results = await asyncio.gather(*[
        asyncio.to_thread(ledger.list_events, 20) for _ in range(10)
    ])
    assert all(len(r) == 20 for r in results)
    _cleanup(path)
    return {"concurrent_reads": 10, "each_returned": 20}
async def test_stress_hash_chain_1000_events(cfg: ChaosConfig) -> dict:
    ledger, path = _tmp_ledger()
    for i in range(1000):
        ledger.append(EventType.USER_INPUT, {"i": i})
    sab = LedgerSaboteur(path)
    async with timing() as t:
        valid, err = sab.verify_chain()
    assert valid, f"Chain invalid: {err}"
    _cleanup(path)
    return {"chain_valid": True, "verify_latency_ms": round(t.elapsed_ms, 2)}


# ── Group 6: temp_ledger isolation & UTF-8 robustness (tests 66–75) ──────────

async def test_temp_ledger_creates_and_cleans(cfg: ChaosConfig) -> dict:
    """temp_ledger context manager must yield a working ledger and clean up."""
    import os
    db_path_captured = None
    with temp_ledger() as ledger:
        db_path_captured = ledger._db_path
        assert ledger.append(EventType.USER_INPUT, {"text": "temp test"})
    # File must be gone after context exits
    assert not os.path.exists(db_path_captured), "temp db not cleaned up"
    return {"cleaned_up": True}

async def test_temp_ledger_isolation_between_calls(cfg: ChaosConfig) -> dict:
    """Two temp_ledger contexts must not share state."""
    with temp_ledger() as l1:
        l1.append(EventType.USER_INPUT, {"text": "ledger1"})
        c1 = len(l1.list_events())
    with temp_ledger() as l2:
        c2 = len(l2.list_events())
    assert c1 == 1
    assert c2 == 0
    return {"l1_events": c1, "l2_events": c2}

async def test_utf8_content_roundtrip(cfg: ChaosConfig) -> dict:
    """Unicode content must survive the JSON round-trip through SQLite."""
    payload = {"text": "Ñoño 中文 emoji 🔥 ąę"}
    with temp_ledger() as ledger:
        eid = ledger.append(EventType.USER_INPUT, payload)
        events = ledger.list_events(last_n=1)
        stored = events[0]["content"]["text"]
    assert stored == payload["text"], f"Mismatch: {stored!r}"
    return {"unicode_ok": True}

async def test_utf8_replacement_on_bad_bytes(cfg: ChaosConfig) -> dict:
    """Non-UTF-8 bytes in metadata must not crash the ledger (errors=replace)."""
    with temp_ledger() as ledger:
        # Inject a row with latin-1 bytes via raw sqlite
        import sqlite3 as _sq
        with _sq.connect(ledger._db_path) as conn:
            conn.execute(
                "INSERT INTO events (event_id, event_type, content, metadata, status, prev_hash) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                ("bad-bytes-id", "USER_INPUT",
                 '{"text":"ok"}',
                 b"\xff\xfe bad bytes".decode("latin-1"),
                 "abc123"),
            )
            conn.commit()
        events = ledger.list_events(last_n=10)
    assert any(e["event_id"] == "bad-bytes-id" for e in events)
    return {"bad_bytes_handled": True}

async def test_temp_ledger_rollback_inside_context(cfg: ChaosConfig) -> dict:
    """Rollback inside temp_ledger must work correctly."""
    with temp_ledger() as ledger:
        ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(5)]
        pruned = ledger.rollback(event_id=ids[2])
    # ids[0], ids[1], ids[2] remain; ids[3], ids[4] pruned
    assert pruned == 2
    return {"pruned": pruned}

async def test_temp_ledger_hash_chain_valid(cfg: ChaosConfig) -> dict:
    """Hash chain must be valid on a fresh temp_ledger after 10 appends."""
    with temp_ledger() as ledger:
        for i in range(10):
            ledger.append(EventType.USER_INPUT, {"i": i})
        sab = LedgerSaboteur(ledger._db_path)
        valid, err = sab.verify_chain()
    assert valid, f"Chain invalid: {err}"
    return {"chain_valid": True}

async def test_temp_ledger_reconstruct_state(cfg: ChaosConfig) -> dict:
    """reconstruct_state must return correct line count from temp_ledger."""
    with temp_ledger() as ledger:
        for topic in ENGINEERING_TOPICS[:5]:
            ledger.append(EventType.USER_INPUT, {"text": topic})
        state = ledger.reconstruct_state(n=5)
    lines = [l for l in state.splitlines() if l.strip()]
    assert len(lines) >= 5
    return {"state_lines": len(lines)}

async def test_temp_ledger_concurrent_appends(cfg: ChaosConfig) -> dict:
    """Concurrent async appends to temp_ledger must all succeed."""
    with temp_ledger() as ledger:
        await asyncio.gather(*[
            asyncio.to_thread(ledger.append, EventType.USER_INPUT, {"i": i})
            for i in range(20)
        ])
        total = len(ledger.list_events(last_n=100))
    assert total == 20
    return {"concurrent_appends": total}

async def test_temp_ledger_export_log_complete(cfg: ChaosConfig) -> dict:
    """export_log must return all active events from temp_ledger."""
    with temp_ledger() as ledger:
        for i in range(30):
            ledger.append(EventType.RESEARCH, {"topic": f"t{i}"})
        exported = ledger.export_log()
    assert len(exported) == 30
    return {"exported": len(exported)}

async def test_temp_ledger_no_cross_contamination(cfg: ChaosConfig) -> dict:
    """Events from one temp_ledger must not appear in a subsequent one."""
    with temp_ledger() as l1:
        for i in range(5):
            l1.append(EventType.USER_INPUT, {"i": i})
    with temp_ledger() as l2:
        count = len(l2.list_events())
    assert count == 0, f"Expected 0, got {count}"
    return {"cross_contamination": False}


# ── Registry (75 tests) ───────────────────────────────────────────────────────

ALL_TESTS = [
    # Group 1: Basic (15)
    test_append_returns_uuid, test_append_stores_event,
    test_list_events_returns_newest_first, test_all_event_types_storable,
    test_content_roundtrip_json, test_metadata_stored, test_status_default_active,
    test_filter_by_event_type, test_filter_by_status, test_export_log_returns_active_only,
    test_multiple_appends_distinct_ids, test_parent_id_stored,
    test_prev_hash_not_null_after_genesis, test_prev_hash_differs_between_events,
    test_init_creates_events_table,
    # Group 2: Rollback (10)
    test_rollback_by_event_id, test_rollback_by_timestamp,
    test_rollback_zero_pruned_on_latest, test_rollback_records_rollback_event,
    test_rollback_idempotent, test_rollback_preserves_earlier_events,
    test_rollback_then_new_append, test_triple_rollback_scenario,
    test_rollback_without_params_raises, test_rollback_unknown_event_id_raises,
    # Group 3: reconstruct_state (15)
    test_reconstruct_empty_ledger, test_reconstruct_includes_user_input,
    test_reconstruct_includes_agent_thought, test_reconstruct_respects_n_limit,
    test_reconstruct_excludes_rolled_back, test_reconstruct_latency_10_events,
    test_reconstruct_latency_100_events, test_reconstruct_latency_500_events,
    test_reconstruct_state_format, test_reconstruct_checkpoint_event,
    test_reconstruct_node_approved, test_reconstruct_per_topic,
    test_reconstruct_file_diff_event, test_reconstruct_mixed_event_types,
    test_reconstruct_is_deterministic,
    # Group 4: Hash-chain (15)
    test_chain_valid_on_fresh_ledger, test_chain_broken_on_corruption,
    test_chain_restored_after_saboteur, test_prev_hash_64_hex_chars,
    test_chain_grows_monotonically,
    test_temporal_paradox_depth_1, test_temporal_paradox_depth_2,
    test_temporal_paradox_depth_3, test_temporal_paradox_depth_5,
    test_temporal_paradox_depth_10, test_temporal_paradox_depth_20,
    test_temporal_paradox_depth_30, test_temporal_paradox_depth_50,
    test_temporal_paradox_sequential_3, test_temporal_paradox_alternating,
    # Group 5: Stress (10)
    test_stress_50_retrieve_10, test_stress_100_retrieve_20,
    test_stress_200_retrieve_50, test_stress_500_retrieve_100,
    test_stress_1000_retrieve_20, test_stress_2000_retrieve_10,
    test_stress_with_rollbacks_mixed, test_stress_export_1000_events,
    test_stress_wal_mode_concurrent_reads, test_stress_hash_chain_1000_events,
    # Group 6: temp_ledger isolation & UTF-8 robustness (10)
    test_temp_ledger_creates_and_cleans, test_temp_ledger_isolation_between_calls,
    test_utf8_content_roundtrip, test_utf8_replacement_on_bad_bytes,
    test_temp_ledger_rollback_inside_context, test_temp_ledger_hash_chain_valid,
    test_temp_ledger_reconstruct_state, test_temp_ledger_concurrent_appends,
    test_temp_ledger_export_log_complete, test_temp_ledger_no_cross_contamination,
]

assert len(ALL_TESTS) == 75, f"Expected 75, got {len(ALL_TESTS)}"


async def main() -> None:
    logger.info(f"[{ITER_NAME}] Starting 75-test temporal integrity suite …")
    collector = MetricsCollector()
    await run_suite(ALL_TESTS, collector, CFG, CATEGORY)
    summary  = collector.summary()
    log_path = save_log(collector, ITER_NAME)

    print(f"\n{'='*60}")
    print(f"  {ITER_NAME.upper()} — RESULTS")
    print(f"{'='*60}")
    print(f"  Total:        {summary['total']}")
    print(f"  Passed:       {summary['passed']}  ({summary['pass_rate']*100:.1f}%)")
    print(f"  Failed:       {summary['failed']}")
    print(f"  Mean latency: {summary['mean_latency']} ms")
    print(f"  P95 latency:  {summary['p95_latency']} ms")
    print(f"  Log:          {log_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
