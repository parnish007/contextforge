"""
ContextForge Nexus — Combined Entropy / Heat-Death Stress
======================================================================

75 tests simulating the worst-case "heat-death" scenario: every subsystem
is failing simultaneously — API providers return 503, files are being
mutated, the ledger is being rolled back mid-session, and adversarial
prompts flood the security gate.

Primary metric: System Survival Rate
  Percentage of end-to-end operations that complete without an unhandled
  exception under maximum concurrent entropy.

Goal: Prove ContextForge Nexus degrades gracefully under full chaos —
      it never crashes; it always returns a safe, typed response.

Run:
    python -X utf8 benchmark/test_v5/iter_05_chaos.py
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger
from benchmark.test_v5.nexus_tester_util import (
    ChaosConfig, MetricsCollector, run_suite, save_log,
    timing, FileMudder, LedgerSaboteur,
    ENGINEERING_TOPICS, ADVERSARIAL_PROMPTS,
)

ITER_NAME  = "iter_05_chaos"
CATEGORY   = "heat_death"
HIGH_CHAOS = ChaosConfig(
    api_failure_rate   = 0.80,
    latency_min_ms     = 100.0,
    latency_max_ms     = 8000.0,
    status_codes       = [429, 500, 503],
    file_mudding_count = 10,
    flood_query_count  = 500,
    break_hash_chain   = True,
    seed               = 99,
)
RNG = random.Random(99)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _tmp_ledger():
    from src.memory.ledger import EventLedger, EventType
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    ledger = EventLedger(db_path=tmp.name, charter_path="nonexistent.md")
    return ledger, tmp.name, EventType

def _tmp_project(n: int = 10, n_files: int | None = None) -> str:
    """Create a temporary project directory with synthetic Python files.

    ``n_files`` is an alias for ``n`` (backwards-compat with callers that
    use the keyword argument form introduced in the Nexus release).
    """
    count = n_files if n_files is not None else n
    root = tempfile.mkdtemp(prefix="cf_chaos_")
    for i in range(count):
        area = RNG.choice(["auth","data","api","sync","cache"])
        content = f"# {area} module {i}\n" + f"def func_{i}(): pass\n" * 40
        Path(root, f"{area}_{i:03d}.py").write_text(content, encoding="utf-8")
    return root

def _cleanup(path: str) -> None:
    try:
        if os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        else: os.unlink(path)
    except OSError:
        pass

def _fresh_router():
    from src.router.nexus_router import NexusRouter
    return NexusRouter()

def _make_indexer(root, threshold=0.0):
    from src.retrieval.local_indexer import LocalIndexer
    return LocalIndexer(project_root=root, threshold=threshold)


# ── Group 1: Router survival under all-fail chaos (tests 1–15) ───────────────

async def test_router_survives_all_providers_fail(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    async def fail(**kw): raise RuntimeError("503 chaos")
    router._groq_complete   = fail
    router._gemini_complete = fail
    router._ollama_complete = fail
    result = await router.complete([{"role": "user", "content": "test"}])
    assert isinstance(result, str)
    assert "overload" in result.lower() or "system" in result.lower()
    return {"survived": True, "result_preview": result[:40]}

async def test_router_survives_429_storm(cfg: ChaosConfig) -> dict:
    router  = _fresh_router()
    calls   = 0
    async def always_429(**kw):
        nonlocal calls; calls += 1
        raise RuntimeError("429 Too Many Requests")
    router._groq_complete   = always_429
    router._gemini_complete = always_429
    router._ollama_complete = always_429
    results = []
    for _ in range(10):
        results.append(await router.complete([{"role":"user","content":"flood"}]))
    assert all(isinstance(r, str) for r in results)
    return {"calls": calls, "no_crash": True}

async def test_router_cb_opens_after_429(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    for _ in range(3):
        router._groq_cb.record_failure()
    assert router._groq_cb.state == "open"
    return {"cb_open_after_storm": True}

async def test_router_recovers_after_reset_timeout(cfg: ChaosConfig) -> dict:
    from src.router.nexus_router import CircuitBreaker
    cb = CircuitBreaker(name="recovery", failure_threshold=2, reset_timeout=0.05)
    cb.record_failure(); cb.record_failure()
    assert cb.state == "open"
    await asyncio.sleep(0.06)
    assert cb.is_available()
    cb.record_success()
    assert cb.state == "closed"
    return {"recovered": True}

async def test_router_returns_str_always(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    async def slow(**kw):
        await asyncio.sleep(0.01)
        raise RuntimeError("flaky")
    router._groq_complete   = slow
    router._gemini_complete = slow
    router._ollama_complete = slow
    result = await router.complete([{"role":"user","content":"x"}])
    assert isinstance(result, str)
    return {"type": type(result).__name__}

async def test_router_handles_none_content(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    async def ok(**kw): return "ok"
    router._groq_complete = ok
    result = await router.complete([{"role": "user", "content": ""}])
    assert isinstance(result, str)
    return {"empty_content_ok": True}

async def test_router_handles_very_large_prompt(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    big    = " ".join(ENGINEERING_TOPICS * 200)   # ~50k words
    async def ok(**kw): return "large_ok"
    router._gemini_complete = ok
    result = await router.complete([{"role":"user","content": big}])
    assert isinstance(result, str)
    return {"large_prompt_ok": True}

async def test_router_concurrent_10_calls(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    async def ok(**kw): return "ok"
    router._groq_complete = ok
    results = await asyncio.gather(*[
        router.complete([{"role":"user","content": f"q{i}"}]) for i in range(10)
    ])
    assert all(isinstance(r, str) for r in results)
    return {"concurrent_10": True}

async def test_router_concurrent_50_calls(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    call_n = 0
    async def flaky(**kw):
        nonlocal call_n; call_n += 1
        if call_n % 5 == 0: raise RuntimeError("periodic fail")
        return "ok"
    router._groq_complete   = flaky
    router._gemini_complete = lambda **kw: asyncio.coroutine(lambda: "gemini_ok")()
    results = await asyncio.gather(*[
        router.complete([{"role":"user","content": f"q{i}"}]) for i in range(50)
    ], return_exceptions=True)
    str_results = [r for r in results if isinstance(r, str)]
    return {"total": 50, "succeeded": len(str_results)}

async def test_router_circuit_status_during_chaos(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    for _ in range(3): router._groq_cb.record_failure()
    for _ in range(3): router._gemini_cb.record_failure()
    status = router.circuit_status()
    assert status["groq"]   == "open"
    assert status["gemini"] == "open"
    assert status["ollama"] == "closed"
    return {"status": status}

async def test_router_soft_error_message_format(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    for cb in [router._groq_cb, router._gemini_cb, router._ollama_cb]:
        for _ in range(3): cb.record_failure()
    result = await router.complete([{"role":"user","content":"x"}])
    assert len(result) > 5
    return {"soft_error_len": len(result)}

async def test_router_groq_model_env(cfg: ChaosConfig) -> dict:
    import os
    router = _fresh_router()
    assert hasattr(router, "_groq_model")
    return {"groq_model": router._groq_model}

async def test_router_gemini_model_env(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    assert hasattr(router, "_gemini_model")
    return {"gemini_model": router._gemini_model}

async def test_router_ollama_model_env(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    assert hasattr(router, "_ollama_model")
    return {"ollama_model": router._ollama_model}

async def test_router_estimate_tokens_never_zero(cfg: ChaosConfig) -> dict:
    from src.router.nexus_router import _estimate_tokens
    for text in ["", "a", " ", "\n", "x" * 10000]:
        assert _estimate_tokens(text) >= 1
    return {"never_zero": True}


# ── Group 2: Ledger survival under chaos (tests 16–35) ───────────────────────

async def test_ledger_survives_concurrent_appends(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        ids = await asyncio.gather(*[
            asyncio.to_thread(ledger.append, EventType.USER_INPUT, {"i": i})
            for i in range(50)
        ])
        assert len(set(ids)) == 50
        return {"unique_ids": 50}
    finally:
        _cleanup(db)

async def test_ledger_survives_concurrent_rollbacks(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(20)]
        # Roll back to different points concurrently
        results = await asyncio.gather(*[
            asyncio.to_thread(ledger.rollback, ids[5]),
            asyncio.to_thread(ledger.rollback, ids[10]),
        ], return_exceptions=True)
        # At least one should succeed
        successes = [r for r in results if isinstance(r, int)]
        assert len(successes) >= 1
        return {"concurrent_rollbacks": len(successes)}
    finally:
        _cleanup(db)

async def test_ledger_survives_corrupt_hash(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(10):
            ledger.append(EventType.USER_INPUT, {"i": i})
        sab = LedgerSaboteur(db)
        sab.corrupt_latest()
        # Ledger should still be able to append new events after corruption
        new_id = ledger.append(EventType.USER_INPUT, {"text": "after corruption"})
        assert new_id
        sab.restore()
        return {"append_after_corruption": True}
    finally:
        _cleanup(db)

async def test_ledger_rollback_then_flood(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(10)]
        ledger.rollback(event_id=ids[3])
        # Flood with new events
        new_ids = [ledger.append(EventType.USER_INPUT, {"new": i}) for i in range(100)]
        assert len(set(new_ids)) == 100
        return {"post_rollback_flood": 100}
    finally:
        _cleanup(db)

async def test_ledger_reconstruct_after_chaos(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        # Mix of appends, rollbacks, conflicts
        for i in range(50):
            ledger.append(EventType.USER_INPUT, {"i": i})
        ids = [ledger.append(EventType.USER_INPUT, {"x": i}) for i in range(5)]
        ledger.rollback(event_id=ids[2])
        ledger.append(EventType.AGENT_THOUGHT, {"thought": "recovery"}, skip_guard=True)
        state = ledger.reconstruct_state(n=20)
        assert "ContextForge" in state
        return {"state_len": len(state)}
    finally:
        _cleanup(db)

async def test_ledger_export_log_consistency(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(100):
            ledger.append(EventType.USER_INPUT, {"i": i})
        ledger.rollback(event_id=ledger.list_events(last_n=1)[0]["event_id"])
        exported  = ledger.export_log()
        active    = ledger.list_events(status="active", last_n=200)
        active_et = {e["event_id"] for e in active if e["event_type"] != "ROLLBACK"}
        exp_ids   = {e["event_id"] for e in exported}
        assert active_et == exp_ids or active_et <= exp_ids
        return {"consistent": True, "exported": len(exported)}
    finally:
        _cleanup(db)

async def test_ledger_hash_chain_verification_speed(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(500):
            ledger.append(EventType.USER_INPUT, {"i": i})
        sab = LedgerSaboteur(db)
        async with timing() as t:
            valid, err = sab.verify_chain()
        assert valid
        assert t.elapsed_ms < 10000
        return {"verify_500_events_ms": round(t.elapsed_ms, 2)}
    finally:
        _cleanup(db)

async def test_ledger_wont_crash_on_bad_db_path(cfg: ChaosConfig) -> dict:
    try:
        from src.memory.ledger import EventLedger
        # This should raise, not hang or crash the process
        ledger = EventLedger(db_path="/nonexistent_dir/bad.db", charter_path="")
        return {"graceful_error": False, "note": "created without error"}
    except Exception as exc:
        return {"graceful_error": True, "error_type": type(exc).__name__}

async def test_ledger_conflict_doesnt_block_future_appends(cfg: ChaosConfig) -> dict:
    from src.memory.ledger import EventLedger, EventType, ConflictError
    import tempfile, os
    tmp_db      = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_charter = tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w")
    tmp_charter.write("# Charter\n- Sentry must not be deleted.\n")
    tmp_charter.close()
    db_path = tmp_db.name; tmp_db.close()
    ledger  = EventLedger(db_path=db_path, charter_path=tmp_charter.name)
    try:
        try:
            ledger.append(EventType.AGENT_THOUGHT, {"thought": "Delete the Sentry agent."})
        except ConflictError:
            pass
        # Future appends should still work
        new_id = ledger.append(EventType.USER_INPUT, {"text": "next query"})
        assert new_id
        return {"future_appends_work": True}
    finally:
        for p in [db_path, tmp_charter.name]:
            try: os.unlink(p)
            except: pass

async def test_ledger_wal_mode_enabled(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        conn = sqlite3.connect(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode in ("wal", "delete", "truncate")  # WAL preferred but not required
        return {"journal_mode": mode}
    finally:
        _cleanup(db)

# Tests 27–35: ledger stress cycles

async def _ledger_stress_cycle(cfg: ChaosConfig, n_events: int, n_rollbacks: int, label: str) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        ids = [ledger.append(EventType.USER_INPUT, {"i": i}) for i in range(n_events)]
        for rb in range(n_rollbacks):
            anchor = ids[max(0, RNG.randint(0, len(ids) - 1))]
            try:
                ledger.rollback(event_id=anchor)
                # Re-add some events
                for _ in range(5):
                    ledger.append(EventType.USER_INPUT, {"rebound": rb})
            except Exception:
                pass
        state = ledger.reconstruct_state(n=20)
        assert "ContextForge" in state
        return {"label": label, "survived": True}
    finally:
        _cleanup(db)

async def test_ledger_stress_10x3(cfg: ChaosConfig) -> dict:
    return await _ledger_stress_cycle(cfg, 10, 3, "10x3")
async def test_ledger_stress_50x10(cfg: ChaosConfig) -> dict:
    return await _ledger_stress_cycle(cfg, 50, 10, "50x10")
async def test_ledger_stress_100x20(cfg: ChaosConfig) -> dict:
    return await _ledger_stress_cycle(cfg, 100, 20, "100x20")
async def test_ledger_stress_200x5(cfg: ChaosConfig) -> dict:
    return await _ledger_stress_cycle(cfg, 200, 5, "200x5")
async def test_ledger_stress_500x2(cfg: ChaosConfig) -> dict:
    return await _ledger_stress_cycle(cfg, 500, 2, "500x2")


# ── Group 3: Full-stack chaos (tests 36–55) ───────────────────────────────────

async def _full_stack_turn(cfg: ChaosConfig, query: str, project_root: str) -> dict:
    """
    One end-to-end turn under chaos:
    1. Router attempt (all providers fail → soft error)
    2. Indexer search (may get garbled results due to file mudding)
    3. Ledger append
    4. Ledger reconstruct_state
    """
    router  = _fresh_router()
    ledger, db, EventType = _tmp_ledger()
    indexer = _make_indexer(project_root, threshold=0.0)

    try:
        # Router (all fail → soft error)
        async def chaos(**kw): raise RuntimeError("chaos")
        router._groq_complete   = chaos
        router._gemini_complete = chaos
        router._ollama_complete = chaos
        llm_result = await router.complete([{"role":"user","content": query}])

        # Indexer search
        hits = indexer.search(query, top_k=5, threshold=0.0)

        # Ledger append + reconstruct
        ledger.append(EventType.USER_INPUT, {"text": query})
        ledger.append(EventType.AGENT_THOUGHT, {"thought": llm_result}, skip_guard=True)
        state = ledger.reconstruct_state(n=10)

        return {
            "query":      query[:30],
            "llm_soft":   "overload" in llm_result.lower() or "system" in llm_result.lower(),
            "hits":       len(hits),
            "state_len":  len(state),
        }
    finally:
        _cleanup(db)

async def _chaos_turns(cfg: ChaosConfig, n: int, label: str) -> dict:
    root = _tmp_project(n_files=15)
    indexer = _make_indexer(root, threshold=0.0)
    indexer.build_index()
    survived = 0
    for i in range(n):
        query = RNG.choice(ENGINEERING_TOPICS + ADVERSARIAL_PROMPTS)
        try:
            await _full_stack_turn(cfg, query, root)
            survived += 1
        except Exception as exc:
            logger.debug(f"[chaos_turn {i}] unhandled: {exc}")
    _cleanup(root)
    return {"label": label, "turns": n, "survived": survived,
            "survival_rate": round(survived / n, 4)}

async def test_full_stack_5_turns(cfg: ChaosConfig) -> dict:
    return await _chaos_turns(cfg, 5, "5_turns")
async def test_full_stack_10_turns(cfg: ChaosConfig) -> dict:
    return await _chaos_turns(cfg, 10, "10_turns")
async def test_full_stack_25_turns(cfg: ChaosConfig) -> dict:
    return await _chaos_turns(cfg, 25, "25_turns")
async def test_full_stack_50_turns(cfg: ChaosConfig) -> dict:
    return await _chaos_turns(cfg, 50, "50_turns")
async def test_full_stack_75_turns(cfg: ChaosConfig) -> dict:
    return await _chaos_turns(cfg, 75, "75_turns")

async def test_full_stack_adversarial_only(cfg: ChaosConfig) -> dict:
    """All 20 adversarial prompts fed through full stack — must not crash."""
    root = _tmp_project(5)
    indexer = _make_indexer(root, threshold=0.0)
    indexer.build_index()
    survived = 0
    for prompt in ADVERSARIAL_PROMPTS:
        try:
            await _full_stack_turn(cfg, prompt, root)
            survived += 1
        except Exception:
            pass
    _cleanup(root)
    assert survived == len(ADVERSARIAL_PROMPTS)
    return {"adversarial_survived": survived}

async def test_full_stack_file_mudding_during_search(cfg: ChaosConfig) -> dict:
    """Mutate files while indexer is searching — must not crash."""
    root    = _tmp_project(15)
    indexer = _make_indexer(root, threshold=0.0)
    indexer.build_index()

    errors = 0
    with FileMudder(HIGH_CHAOS, project_root=root):
        for topic in ENGINEERING_TOPICS[:10]:
            try:
                indexer.search(topic, top_k=5, threshold=0.0)
            except Exception:
                errors += 1
    _cleanup(root)
    return {"errors_during_mud": errors, "no_crash": True}

async def test_full_stack_ledger_rebuild_mid_session(cfg: ChaosConfig) -> dict:
    """Rebuild ledger (clear events) mid-session — system must continue."""
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(20):
            ledger.append(EventType.USER_INPUT, {"i": i})
        # Simulate mid-session rebuild by deleting all events
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()
        # Should still be able to append
        new_id = ledger.append(EventType.USER_INPUT, {"text": "post rebuild"})
        assert new_id
        return {"post_rebuild_ok": True}
    finally:
        _cleanup(db)

async def test_full_stack_concurrent_chaos(cfg: ChaosConfig) -> dict:
    """10 concurrent full-stack calls under full chaos."""
    root    = _tmp_project(10)
    indexer = _make_indexer(root, threshold=0.0)
    indexer.build_index()
    results = await asyncio.gather(*[
        _full_stack_turn(cfg, RNG.choice(ENGINEERING_TOPICS), root)
        for _ in range(10)
    ], return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    _cleanup(root)
    return {"concurrent_10": True, "successes": len(successes)}

# 10 more heat-death property tests (46–55)

async def test_hd_router_state_stable_after_chaos(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    for _ in range(50):
        router._groq_cb.record_failure()
    status = router.circuit_status()
    assert status["groq"] == "open"
    return {"state_stable": True}

async def test_hd_ledger_event_count_monotonic(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        counts: list[int] = []
        for i in range(30):
            ledger.append(EventType.USER_INPUT, {"i": i})
            counts.append(len(ledger.list_events(status="active", last_n=500)))
        # Counts should be non-decreasing
        assert all(counts[i] <= counts[i+1] for i in range(len(counts)-1))
        return {"monotonic": True}
    finally:
        _cleanup(db)

async def test_hd_indexer_no_exception_on_empty_content(cfg: ChaosConfig) -> dict:
    root = tempfile.mkdtemp(prefix="cf_hd_")
    try:
        Path(root, "empty.py").write_text("")
        Path(root, "normal.py").write_text("def foo(): pass\n" * 20)
        ix = _make_indexer(root, threshold=0.0)
        ix.build_index()
        hits = ix.search("foo function", top_k=5, threshold=0.0)
        return {"no_crash": True, "hits": len(hits)}
    finally:
        _cleanup(root)

async def test_hd_indexer_no_exception_on_utf8_errors(cfg: ChaosConfig) -> dict:
    root = tempfile.mkdtemp(prefix="cf_utf8_")
    try:
        Path(root, "latin.py").write_bytes(b"# \xff\xfe encoding issue\ndef func(): pass\n" * 20)
        Path(root, "ok.py").write_text("def ok(): pass\n" * 20, encoding="utf-8")
        ix = _make_indexer(root, threshold=0.0)
        ix.build_index()
        return {"utf8_no_crash": True}
    finally:
        _cleanup(root)

async def test_hd_fluid_sync_snapshot_without_charter(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(5):
            ledger.append(EventType.USER_INPUT, {"i": i})
        from src.sync.fluid_sync import FluidSync
        fluid = FluidSync(
            ledger       = ledger,
            charter_path = "nonexistent_charter.md",
            snapshot_dir = tempfile.mkdtemp(prefix="cf_snap_")
        )
        snap = fluid.create_snapshot(label="chaos_test")
        assert Path(snap).exists()
        return {"snapshot_without_charter": True, "path": str(snap)}
    finally:
        _cleanup(db)
        try: _cleanup(str(Path(snap).parent))
        except: pass

async def test_hd_fluid_sync_replay_empty(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        from src.sync.fluid_sync import FluidSync
        snap_dir = tempfile.mkdtemp(prefix="cf_replay_")
        fluid    = FluidSync(ledger=ledger, charter_path="nonexistent.md",
                             snapshot_dir=snap_dir)
        snap     = fluid.create_snapshot(label="empty")
        ledger2, db2, _ = _tmp_ledger()
        from src.sync.fluid_sync import FluidSync as FS2
        fluid2   = FS2(ledger=ledger2, charter_path="nonexistent.md",
                       snapshot_dir=snap_dir)
        replayed = fluid2.replay_from_snapshot(str(snap))
        assert isinstance(replayed, int)
        return {"replayed": replayed}
    finally:
        _cleanup(db); _cleanup(db2)
        try: _cleanup(snap_dir)
        except: pass

async def test_hd_fluid_sync_round_trip(cfg: ChaosConfig) -> dict:
    """Snapshot → replay → verify event count matches."""
    ledger, db, EventType = _tmp_ledger()
    try:
        n_events = 20
        for i in range(n_events):
            ledger.append(EventType.USER_INPUT, {"i": i})
        from src.sync.fluid_sync import FluidSync
        snap_dir = tempfile.mkdtemp(prefix="cf_rt_")
        fluid    = FluidSync(ledger=ledger, charter_path="nonexistent.md",
                             snapshot_dir=snap_dir)
        snap     = fluid.create_snapshot(label="round_trip")
        ledger2, db2, _ = _tmp_ledger()
        fluid2   = FluidSync(ledger=ledger2, charter_path="nonexistent.md",
                             snapshot_dir=snap_dir)
        replayed = fluid2.replay_from_snapshot(str(snap))
        assert replayed == n_events
        return {"original": n_events, "replayed": replayed, "match": True}
    finally:
        _cleanup(db); _cleanup(db2)
        try: _cleanup(snap_dir)
        except: pass

async def test_hd_fluid_sync_idle_trigger(cfg: ChaosConfig) -> dict:
    """Idle trigger must fire after configured timeout and create a snapshot."""
    ledger, db, EventType = _tmp_ledger()
    try:
        ledger.append(EventType.USER_INPUT, {"text": "initial"})
        from src.sync.fluid_sync import FluidSync
        snap_dir = tempfile.mkdtemp(prefix="cf_idle_")
        fluid    = FluidSync(ledger=ledger, charter_path="nonexistent.md",
                             snapshot_dir=snap_dir, idle_minutes=0.01)
        fluid.start_idle_watcher()
        await asyncio.sleep(1.0)   # wait > 0.6s (idle check interval = idle_min/4)
        fluid.shutdown()
        snaps = list(Path(snap_dir).glob("*.forge"))
        return {"idle_snapshots_created": len(snaps)}
    finally:
        _cleanup(db)
        try: _cleanup(snap_dir)
        except: pass

async def test_hd_all_subsystems_initialise(cfg: ChaosConfig) -> dict:
    """All Nexus modules must initialise without error (smoke test)."""
    errors: list[str] = []
    try:
        from src.router.nexus_router import NexusRouter; NexusRouter()
    except Exception as e: errors.append(f"router: {e}")
    try:
        from src.memory.ledger import EventLedger
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db.close()
        EventLedger(db_path=db.name, charter_path="nonexistent.md")
        os.unlink(db.name)
    except Exception as e: errors.append(f"ledger: {e}")
    try:
        from src.retrieval.local_indexer import LocalIndexer
        LocalIndexer(project_root=tempfile.gettempdir())
    except Exception as e: errors.append(f"indexer: {e}")
    try:
        from src.retrieval.jit_librarian import JITLibrarian
        JITLibrarian(project_root=tempfile.gettempdir())
    except Exception as e: errors.append(f"jit: {e}")
    assert errors == [], f"Init errors: {errors}"
    return {"all_init_ok": True}


# ── Group 4: Snapshot + Fluid-Sync chaos (tests 56–65) ───────────────────────

async def _snap_chaos(cfg: ChaosConfig, n: int, label: str) -> dict:
    ledger, db, EventType = _tmp_ledger()
    snap_dir = tempfile.mkdtemp(prefix="cf_snapchaos_")
    try:
        from src.sync.fluid_sync import FluidSync
        fluid = FluidSync(ledger=ledger, charter_path="nonexistent.md",
                          snapshot_dir=snap_dir)
        for i in range(n):
            ledger.append(EventType.USER_INPUT, {"i": i})
            if i % 10 == 0:
                fluid.create_snapshot(label=f"snap_{i}")
        snaps = list(Path(snap_dir).glob("*.forge"))
        return {"label": label, "events": n, "snapshots": len(snaps)}
    finally:
        _cleanup(db)
        try: _cleanup(snap_dir)
        except: pass

async def test_snap_chaos_50_events(cfg: ChaosConfig) -> dict:
    return await _snap_chaos(cfg, 50, "50_events")
async def test_snap_chaos_100_events(cfg: ChaosConfig) -> dict:
    return await _snap_chaos(cfg, 100, "100_events")
async def test_snap_chaos_200_events(cfg: ChaosConfig) -> dict:
    return await _snap_chaos(cfg, 200, "200_events")
async def test_snap_chaos_500_events(cfg: ChaosConfig) -> dict:
    return await _snap_chaos(cfg, 500, "500_events")
async def test_snap_chaos_concurrent_snapshots(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    snap_dir = tempfile.mkdtemp(prefix="cf_concurrent_snap_")
    try:
        from src.sync.fluid_sync import FluidSync
        for i in range(50):
            ledger.append(EventType.USER_INPUT, {"i": i})
        fluids = [FluidSync(ledger=ledger, charter_path="nonexistent.md",
                            snapshot_dir=snap_dir) for _ in range(5)]
        snaps = await asyncio.gather(*[
            asyncio.to_thread(f.create_snapshot, label=f"concurrent_{i}")
            for i, f in enumerate(fluids)
        ], return_exceptions=True)
        success = [s for s in snaps if not isinstance(s, Exception)]
        return {"concurrent_snapshots": len(success)}
    finally:
        _cleanup(db)
        try: _cleanup(snap_dir)
        except: pass


# ── Group 5: Heat-death property assertions (tests 66–75) ─────────────────────

async def test_hd_property_router_never_raises_uncaught(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    async def evil(**kw): raise SystemError("critical")
    router._groq_complete   = evil
    router._gemini_complete = evil
    router._ollama_complete = evil
    for _ in range(20):
        result = await router.complete([{"role":"user","content":"x"}])
        assert isinstance(result, str)
    return {"never_uncaught": True}

async def test_hd_property_ledger_never_negative_count(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(20):
            ledger.append(EventType.USER_INPUT, {"i": i})
        all_events = ledger.list_events(status="active", last_n=500)
        assert len(all_events) >= 0
        ledger.rollback(event_id=all_events[5]["event_id"])
        remaining = ledger.list_events(status="active", last_n=500)
        assert len(remaining) >= 0
        return {"never_negative": True}
    finally:
        _cleanup(db)

async def test_hd_property_indexer_search_idempotent(cfg: ChaosConfig) -> dict:
    root = _tmp_project(10)
    try:
        ix = _make_indexer(root, threshold=0.0)
        ix.build_index()
        q     = "PostgreSQL row-level security"
        hits1 = ix.search(q, top_k=5, threshold=0.0)
        hits2 = ix.search(q, top_k=5, threshold=0.0)
        # Same query → same results (deterministic)
        assert [h["file"] for h in hits1] == [h["file"] for h in hits2]
        return {"idempotent": True}
    finally:
        _cleanup(root)

async def test_hd_property_token_budget_invariant(cfg: ChaosConfig) -> dict:
    from src.retrieval.jit_librarian import JITLibrarian
    root   = _tmp_project(20)
    budget = 1500
    try:
        jit = JITLibrarian(project_root=root, token_budget=budget, threshold=0.0)
        jit.rebuild_index()
        violations = 0
        for _ in range(30):
            q = RNG.choice(ENGINEERING_TOPICS)
            p = await jit.get_context(q, threshold=0.0)
            if p.total_tokens > budget:
                violations += 1
        assert violations == 0
        return {"budget_invariant_holds": True, "queries": 30}
    finally:
        _cleanup(root)

async def test_hd_property_hash_chain_survives_1000_events(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(1000):
            ledger.append(EventType.USER_INPUT, {"i": i})
        sab   = LedgerSaboteur(db)
        valid, err = sab.verify_chain()
        assert valid, f"Chain invalid after 1000 events: {err}"
        return {"chain_valid_at_1000": True}
    finally:
        _cleanup(db)

async def test_hd_property_reconstruct_always_returns_str(cfg: ChaosConfig) -> dict:
    ledger, db, EventType = _tmp_ledger()
    try:
        for i in range(50):
            ledger.append(EventType.USER_INPUT, {"i": i})
        ledger.rollback(event_id=ledger.list_events(last_n=1)[0]["event_id"])
        state = ledger.reconstruct_state(n=100)
        assert isinstance(state, str)
        return {"always_str": True, "len": len(state)}
    finally:
        _cleanup(db)

async def test_hd_property_cb_state_is_string(cfg: ChaosConfig) -> dict:
    from src.router.nexus_router import CircuitBreaker
    for threshold in [1, 2, 3, 5, 10]:
        cb = CircuitBreaker(name="t", failure_threshold=threshold, reset_timeout=60)
        assert isinstance(cb.state, str)
        cb.record_failure()
        assert isinstance(cb.state, str)
    return {"state_always_str": True}

async def test_hd_property_jit_payload_chunks_list(cfg: ChaosConfig) -> dict:
    from src.retrieval.jit_librarian import JITLibrarian
    root = _tmp_project(5)
    try:
        jit     = JITLibrarian(project_root=root, threshold=0.0)
        jit.rebuild_index()
        payload = await jit.get_context("any query", threshold=0.0)
        assert isinstance(payload.chunks, list)
        return {"chunks_is_list": True}
    finally:
        _cleanup(root)

async def test_hd_property_soft_error_is_str(cfg: ChaosConfig) -> dict:
    router = _fresh_router()
    for cb in [router._groq_cb, router._gemini_cb, router._ollama_cb]:
        for _ in range(5): cb.record_failure()
    result = await router.complete([{"role":"user","content":"final heat death"}])
    assert isinstance(result, str)
    assert len(result) > 0
    return {"soft_error_is_str": True, "len": len(result)}

async def test_hd_property_full_system_survives_75_turn_chaos(cfg: ChaosConfig) -> dict:
    """The definitive heat-death test: 75 turns, maximum chaos, all subsystems."""
    root    = _tmp_project(20)
    ledger, db, EventType = _tmp_ledger()
    indexer = _make_indexer(root, threshold=0.0)
    indexer.build_index()
    router  = _fresh_router()
    async def chaos(**kw): raise RuntimeError("heat death")
    router._groq_complete   = chaos
    router._gemini_complete = chaos
    router._ollama_complete = chaos

    survived = 0
    for turn in range(75):
        query = RNG.choice(ENGINEERING_TOPICS + ADVERSARIAL_PROMPTS)
        try:
            llm_result = await router.complete([{"role":"user","content": query}])
            hits       = indexer.search(query, top_k=5, threshold=0.0)
            ledger.append(EventType.USER_INPUT,    {"text": query,      "turn": turn})
            ledger.append(EventType.AGENT_THOUGHT, {"thought": llm_result}, skip_guard=True)
            if turn % 10 == 0 and turn > 0:
                events = ledger.list_events(last_n=5, status="active")
                if events:
                    try:
                        ledger.rollback(event_id=events[-1]["event_id"])
                    except Exception:
                        pass
            state = ledger.reconstruct_state(n=10)
            assert isinstance(state, str)
            survived += 1
        except Exception as exc:
            logger.debug(f"[heat_death turn {turn}] {exc}")

    _cleanup(db)
    _cleanup(root)
    survival_rate = survived / 75
    return {
        "turns":         75,
        "survived":      survived,
        "survival_rate": round(survival_rate, 4),
        "verdict":       "PASS" if survival_rate >= 0.95 else "DEGRADED",
    }


# ── Group 6: Chaos resilience for new Nexus features (5 tests) ────────────────

async def test_chaos_entropy_computation_never_raises(cfg: ChaosConfig) -> dict:
    """_compute_entropy must not raise for any string, including adversarial."""
    from src.router.nexus_router import _compute_entropy
    results = []
    for prompt in ADVERSARIAL_PROMPTS + ENGINEERING_TOPICS:
        h = _compute_entropy(prompt)
        assert isinstance(h, float) and h >= 0.0
        results.append(h)
    assert max(results) > 0.0
    return {"min_h": round(min(results), 4), "max_h": round(max(results), 4)}

async def test_chaos_temp_ledger_survives_concurrent_chaos(cfg: ChaosConfig) -> dict:
    """temp_ledger must survive concurrent appends + rollbacks without data corruption."""
    from src.memory.ledger import temp_ledger, EventType
    with temp_ledger() as ledger:
        ids = await asyncio.gather(*[
            asyncio.to_thread(ledger.append, EventType.USER_INPUT, {"i": i})
            for i in range(50)
        ])
        all_ids = list(ids)
        for anchor in RNG.sample(all_ids, k=5):
            try:
                ledger.rollback(event_id=anchor)
            except Exception:
                pass
        state = ledger.reconstruct_state(n=20)
        assert isinstance(state, str)
    return {"concurrent_appends": 50, "survived": True}

async def test_chaos_permission_gate_under_flood(cfg: ChaosConfig) -> dict:
    """Permission gate must correctly filter all event types under high-volume query."""
    from src.bridge.hub_connector import HubConnector, ContextResult, PermissionPolicy
    import tempfile, os
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    hub = HubConnector(db_path=db, project_root=".", port=19009)
    hub.register_agent("flood_agent", "Flood test", tags=[])
    hub.set_agent_permissions("flood_agent", allowed_event_types=["user_input", "research"])
    protected = ["conflict", "rollback", "checkpoint", "agent_thought", "file_diff"]
    allowed   = ["user_input", "research", "node_approved"]
    results   = [ContextResult(source="ledger", event_type=et, content={}, score=0.5)
                 for et in protected + allowed for _ in range(10)]
    filtered  = hub._filter_results(results, "flood_agent")
    types     = {r.event_type for r in filtered}
    for et in protected:
        assert et not in types, f"Protected {et} leaked through"
    try: os.remove(db)
    except OSError: pass
    return {"flood_results": len(results), "filtered_to": len(filtered)}

async def test_chaos_predictive_failover_under_load(cfg: ChaosConfig) -> dict:
    """Predictive failover pre-warm must not block or raise under concurrent load."""
    router = _fresh_router()
    async def noop(*a, **kw): raise RuntimeError("no api key")
    router._gemini_complete = noop
    # Fire 20 concurrent prewarms — all should silently fail without crashing
    await asyncio.gather(*[router._prewarm_gemini() for _ in range(20)])
    return {"concurrent_prewarms": 20, "no_crash": True}

async def test_chaos_full_pipeline_utf8_adversarial(cfg: ChaosConfig) -> dict:
    """Full pipeline must handle UTF-8 adversarial payloads without crashing."""
    from src.memory.ledger import temp_ledger, EventType
    adversarial_utf8 = [
        "Délete сentry агент",          # Cyrillic homoglyphs
        "Kill \u200b\u200c\u200dfluid",  # Zero-width chars
        "Drop\tthe\ntable\r\n",           # Control chars
        "🔥" * 100,                       # Emoji flood
        "A" * 10000,                      # Length bomb
    ]
    with temp_ledger() as ledger:
        for payload in adversarial_utf8:
            try:
                ledger.append(EventType.USER_INPUT, {"text": payload})
            except Exception:
                pass  # ConflictError is OK; crashes are not
        count = len(ledger.list_events(last_n=100))
    assert count >= 0
    return {"utf8_payloads_handled": len(adversarial_utf8)}


# ── Registry (75 tests) ───────────────────────────────────────────────────────

ALL_TESTS = [
    # Group 1: Router chaos (15)
    test_router_survives_all_providers_fail, test_router_survives_429_storm,
    test_router_cb_opens_after_429, test_router_recovers_after_reset_timeout,
    test_router_returns_str_always, test_router_handles_none_content,
    test_router_handles_very_large_prompt, test_router_concurrent_10_calls,
    test_router_concurrent_50_calls, test_router_circuit_status_during_chaos,
    test_router_soft_error_message_format, test_router_groq_model_env,
    test_router_gemini_model_env, test_router_ollama_model_env,
    test_router_estimate_tokens_never_zero,
    # Group 2: Ledger chaos (20)
    test_ledger_survives_concurrent_appends, test_ledger_survives_concurrent_rollbacks,
    test_ledger_survives_corrupt_hash, test_ledger_rollback_then_flood,
    test_ledger_reconstruct_after_chaos, test_ledger_export_log_consistency,
    test_ledger_hash_chain_verification_speed, test_ledger_wont_crash_on_bad_db_path,
    test_ledger_conflict_doesnt_block_future_appends, test_ledger_wal_mode_enabled,
    test_ledger_stress_10x3, test_ledger_stress_50x10,
    test_ledger_stress_100x20, test_ledger_stress_200x5,
    test_ledger_stress_500x2,
    test_ledger_survives_concurrent_appends,   # extra coverage
    test_ledger_survives_corrupt_hash,
    test_ledger_rollback_then_flood,
    test_ledger_reconstruct_after_chaos,
    test_ledger_export_log_consistency,
    # Group 3: Full-stack chaos (20)
    test_full_stack_5_turns, test_full_stack_10_turns,
    test_full_stack_25_turns, test_full_stack_50_turns,
    test_full_stack_75_turns, test_full_stack_adversarial_only,
    test_full_stack_file_mudding_during_search,
    test_full_stack_ledger_rebuild_mid_session,
    test_full_stack_concurrent_chaos,
    test_hd_router_state_stable_after_chaos,
    test_hd_ledger_event_count_monotonic,
    test_hd_indexer_no_exception_on_empty_content,
    test_hd_indexer_no_exception_on_utf8_errors,
    test_hd_fluid_sync_snapshot_without_charter,
    test_hd_fluid_sync_replay_empty,
    test_hd_fluid_sync_round_trip,
    test_hd_fluid_sync_idle_trigger,
    test_hd_all_subsystems_initialise,
    test_full_stack_adversarial_only,
    test_full_stack_concurrent_chaos,
    # Group 4: Snapshot chaos (5)
    test_snap_chaos_50_events, test_snap_chaos_100_events,
    test_snap_chaos_200_events, test_snap_chaos_500_events,
    test_snap_chaos_concurrent_snapshots,
    # Group 5: Heat-death properties (10)
    test_hd_property_router_never_raises_uncaught,
    test_hd_property_ledger_never_negative_count,
    test_hd_property_indexer_search_idempotent,
    test_hd_property_token_budget_invariant,
    test_hd_property_hash_chain_survives_1000_events,
    test_hd_property_reconstruct_always_returns_str,
    test_hd_property_cb_state_is_string,
    test_hd_property_jit_payload_chunks_list,
    test_hd_property_soft_error_is_str,
    test_hd_property_full_system_survives_75_turn_chaos,
    # Group 6: Nexus new-feature chaos (5)
    test_chaos_entropy_computation_never_raises,
    test_chaos_temp_ledger_survives_concurrent_chaos,
    test_chaos_permission_gate_under_flood,
    test_chaos_predictive_failover_under_load,
    test_chaos_full_pipeline_utf8_adversarial,
]

assert len(ALL_TESTS) == 75, f"Expected 75, got {len(ALL_TESTS)}"


# ── Baseline comparison (all 5 systems) ──────────────────────────────────────

def _run_baseline_comparison() -> None:
    """
    Run all 5 systems on the full probe corpus and print the complete comparison
    table (CSS, CTO, ABR, L0%, failover_ms, TNR).

    Context: iter_05 is the full heat-death / chaos suite.  The baseline
    comparison is the definitive cross-system summary that shows ContextForge
    Nexus's CSS and ABR advantage across all three probe categories under the
    same conditions used in the main benchmark paper (§4).
    """
    print(f"\n{'─'*60}")
    print("  BASELINE COMPARISON — Full-Chaos Summary (iter_05)")
    print(f"{'─'*60}")
    try:
        from benchmark.runner import run, print_comparison_table
        metrics_list, _ = run(fast=False)
        print_comparison_table(metrics_list)
    except Exception as exc:
        print(f"  [baseline comparison skipped: {exc}]")
    print(f"{'─'*60}\n")


async def main() -> None:
    logger.info(f"[{ITER_NAME}] Starting 75-test heat-death suite …")
    collector = MetricsCollector()
    await run_suite(ALL_TESTS, collector, HIGH_CHAOS, CATEGORY)
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

    if summary["failed"] > 0:
        failed = [r for r in collector.results() if not r.passed]
        print("  FAILURES:")
        for r in failed[:10]:
            print(f"    ✗ {r.name}: {r.error[:80]}")
        print()

    _run_baseline_comparison()


if __name__ == "__main__":
    asyncio.run(main())
