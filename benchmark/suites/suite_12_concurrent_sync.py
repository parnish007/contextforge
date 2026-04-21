# RATIONALE: Tests OR-Set CRDT convergence under concurrent writes from 3 IDE
# clients and split-brain reconnection, replacing the LWW assertion with a
# formal convergence check.
"""
Suite 12 — Concurrent Sync & CRDT Convergence
===============================================

Simulates three IDE clients writing to the same project simultaneously and
verifies that all clients converge to the same final state after sync.

Three test scenarios
────────────────────
  Scenario A — Concurrent adds (no conflict)
      Clients A, B, C each add different nodes while offline.
      After full sync, all three clients must contain all nodes.
      Expected: convergent state with 3+ nodes, no data loss.

  Scenario B — Concurrent updates to the same node
      Clients A and B independently update node_001 while offline.
      After sync, policy determines outcome:
        LWW     → highest-timestamp version wins
        OR_SET  → both versions retained (or dominant version via VC)
        MANUAL  → conflict quarantined

  Scenario C — Split-brain: two clients offline, then reconnect
      1. Start from shared state (node_001).
      2. Client A goes offline; deletes node_001 and adds node_002.
      3. Client B goes offline; updates node_001 and adds node_003.
      4. Both reconnect to Client C (coordinator).
      5. Verify: OR_SET policy keeps node_001 (add wins over remove),
         plus node_002 and node_003 are present on all clients.

Metrics reported
────────────────
  convergence_rate  — fraction of scenarios where all clients reach the same set
  conflict_count    — number of concurrent writes detected
  split_brain_nodes — nodes recovered after split-brain reconnect
  latency_ms        — time for full 3-way merge

Results saved to research/benchmark_results/suite_12_concurrent_sync.json.

Usage
-----
  python -X utf8 benchmark/suites/suite_12_concurrent_sync.py
  python -X utf8 benchmark/suites/suite_12_concurrent_sync.py --verbose
  python -X utf8 benchmark/suites/suite_12_concurrent_sync.py --policy or_set
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="ERROR", format="{message}")

from src.sync.crdt_sync import ORSetSync, ConflictPolicy, VectorClock

RESULTS_DIR = ROOT / "research" / "benchmark_results"
OUT_DEFAULT = RESULTS_DIR / "suite_12_concurrent_sync.json"


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    name:              str
    policy:            str
    passed:            bool
    convergent:        bool
    expected_elements: list[str]
    actual_elements:   list[str]
    conflict_count:    int
    split_brain_nodes: list[str]
    latency_ms:        float
    detail:            str


# ─────────────────────────────────────────────────────────────────────────────
# Helper: 3-way merge (A←B, A←C, B←A, B←C, C←A, C←B)
# ─────────────────────────────────────────────────────────────────────────────

def _full_sync(a: ORSetSync, b: ORSetSync, c: ORSetSync) -> None:
    """Simulate a full gossip round: each client merges from the other two."""
    a.merge(b.export_state())
    a.merge(c.export_state())
    b.merge(a.export_state())
    b.merge(c.export_state())
    c.merge(a.export_state())
    c.merge(b.export_state())


def _check_convergence(
    a: ORSetSync,
    b: ORSetSync,
    c: ORSetSync,
) -> tuple[bool, list[str], list[str]]:
    """
    Check that all three clients have the same present-element set.
    Returns (converged, elements_a, symmetric_diff).
    """
    ea = set(a.elements().keys())
    eb = set(b.elements().keys())
    ec = set(c.elements().keys())
    converged  = ea == eb == ec
    sym_diff   = list((ea ^ eb) | (eb ^ ec) | (ea ^ ec))
    return converged, sorted(ea), sym_diff


# ─────────────────────────────────────────────────────────────────────────────
# Scenario A — Concurrent adds (3 clients, 3 different nodes)
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario_a(policy: ConflictPolicy, verbose: bool) -> ScenarioResult:
    """
    Three clients each add a different node while offline.
    After sync, all clients must have all three nodes.
    """
    t0 = time.monotonic()

    a = ORSetSync("ide_client_a", policy=policy)
    b = ORSetSync("ide_client_b", policy=policy)
    c = ORSetSync("ide_client_c", policy=policy)

    # Offline writes — no sync yet
    a.add("node_jwt_auth",     {"summary": "JWT auth refresh rotation", "confidence": 0.9})
    b.add("node_circuit_break",{"summary": "Circuit breaker CLOSED→OPEN", "confidence": 0.85})
    c.add("node_entropy_gate", {"summary": "Shannon entropy H*=3.5 gate", "confidence": 0.95})

    # Full gossip sync
    _full_sync(a, b, c)

    converged, elements, sym_diff = _check_convergence(a, b, c)
    expected = sorted(["node_jwt_auth", "node_circuit_break", "node_entropy_gate"])

    passed = converged and set(elements) == set(expected)
    latency_ms = (time.monotonic() - t0) * 1000

    if verbose:
        print(f"  Scenario A  converged={converged}  elements={elements}  "
              f"expected={expected}  diff={sym_diff}")

    return ScenarioResult(
        name              = "A_concurrent_adds",
        policy            = policy.value,
        passed            = passed,
        convergent        = converged,
        expected_elements = expected,
        actual_elements   = elements,
        conflict_count    = 0,
        split_brain_nodes = [],
        latency_ms        = round(latency_ms, 2),
        detail            = (
            f"All 3 clients converged with {len(elements)} nodes" if passed
            else f"Convergence failed: sym_diff={sym_diff}"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario B — Concurrent updates to the same node
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario_b(policy: ConflictPolicy, verbose: bool) -> ScenarioResult:
    """
    Clients A and B concurrently update node_001.
    LWW → one wins; OR_SET → dominant VC wins; MANUAL → conflict quarantined.
    """
    t0 = time.monotonic()

    a = ORSetSync("ide_client_a", policy=policy)
    b = ORSetSync("ide_client_b", policy=policy)
    c = ORSetSync("ide_client_c", policy=policy)

    # Shared initial state
    a.add("node_001", {"summary": "Initial JWT auth", "confidence": 0.8})
    b.merge(a.export_state())
    c.merge(a.export_state())

    # Concurrent offline updates
    time.sleep(0.001)  # ensure distinct timestamps for LWW test
    a.add("node_001", {"summary": "JWT auth — updated by A", "confidence": 0.88})
    b.add("node_001", {"summary": "JWT auth — updated by B", "confidence": 0.92})

    # Full sync
    _full_sync(a, b, c)

    converged, elements, sym_diff = _check_convergence(a, b, c)
    conflicts_total = (
        len(a.list_conflicts()) + len(b.list_conflicts()) + len(c.list_conflicts())
    )

    # For OR_SET / LWW: convergent is the pass criterion
    # For MANUAL: conflicts quarantined is the pass criterion
    if policy == ConflictPolicy.MANUAL:
        passed = conflicts_total > 0  # conflict was detected and quarantined
        detail = f"MANUAL quarantine: {conflicts_total} conflict(s) detected"
    else:
        passed = converged
        v = a.get("node_001")
        detail = (
            f"Converged; node_001 = {v.get('summary','?')!r}" if passed
            else f"No convergence: diff={sym_diff}"
        )

    latency_ms = (time.monotonic() - t0) * 1000

    if verbose:
        print(f"  Scenario B  policy={policy.value}  conflicts={conflicts_total}  "
              f"converged={converged}")

    return ScenarioResult(
        name              = "B_concurrent_updates",
        policy            = policy.value,
        passed            = passed,
        convergent        = converged,
        expected_elements = ["node_001"],
        actual_elements   = elements,
        conflict_count    = conflicts_total,
        split_brain_nodes = [],
        latency_ms        = round(latency_ms, 2),
        detail            = detail,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario C — Split-brain: two clients offline, then reconnect
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario_c(policy: ConflictPolicy, verbose: bool) -> ScenarioResult:
    """
    Split-brain: A deletes+adds, B updates+adds, both reconnect to C.

    Under OR_SET semantics, node_001 should survive because B's add-tag
    was not seen by A at the time of A's remove. (Add wins over unseen remove.)
    """
    t0 = time.monotonic()

    # Shared initial state
    coord = ORSetSync("ide_coordinator", policy=policy)
    coord.add("node_001", {"summary": "Shared JWT auth", "confidence": 0.8})

    a = ORSetSync("ide_client_a", policy=policy)
    b = ORSetSync("ide_client_b", policy=policy)
    a.merge(coord.export_state())
    b.merge(coord.export_state())

    # ── Split: A goes offline ─────────────────────────────────────────────
    a.remove("node_001")                   # A deletes the shared node
    a.add("node_002", {"summary": "New node from A", "confidence": 0.9})

    # ── Split: B goes offline ─────────────────────────────────────────────
    b.add("node_001", {"summary": "JWT auth — B update", "confidence": 0.95})
    b.add("node_003", {"summary": "New node from B", "confidence": 0.87})

    # ── Reconnect: A→coord, B→coord, then full 3-way sync ─────────────────
    coord.merge(a.export_state())
    coord.merge(b.export_state())
    a.merge(coord.export_state())
    b.merge(coord.export_state())
    _full_sync(a, b, coord)

    converged, elements, sym_diff = _check_convergence(a, b, coord)
    conflict_count = (
        len(a.list_conflicts()) + len(b.list_conflicts()) + len(coord.list_conflicts())
    )

    # Expected under OR_SET: node_001 survives (B's add wasn't seen by A's remove),
    # plus node_002 and node_003 are present everywhere.
    # Under LWW: depends on timestamps — node_001 may or may not survive.
    recovered = [eid for eid in ["node_001", "node_002", "node_003"] if eid in elements]

    if policy == ConflictPolicy.OR_SET:
        # node_001 must survive (OR-Set add-wins) + node_002, node_003 present
        passed = set(["node_001", "node_002", "node_003"]).issubset(set(elements)) and converged
        detail = (
            f"OR-Set add-wins: all split-brain nodes recovered ({recovered})" if passed
            else f"Unexpected: sym_diff={sym_diff}  recovered={recovered}"
        )
    elif policy == ConflictPolicy.LWW:
        # node_002, node_003 must be present; node_001 outcome is LWW-determined
        passed = set(["node_002", "node_003"]).issubset(set(elements)) and converged
        detail = f"LWW converged; present={elements}"
    else:  # MANUAL
        passed = conflict_count > 0 and converged
        detail = f"MANUAL: {conflict_count} conflict(s); converged={converged}"

    latency_ms = (time.monotonic() - t0) * 1000

    if verbose:
        print(f"  Scenario C  policy={policy.value}  recovered={recovered}  "
              f"converged={converged}  conflicts={conflict_count}")

    return ScenarioResult(
        name              = "C_split_brain",
        policy            = policy.value,
        passed            = passed,
        convergent        = converged,
        expected_elements = sorted({"node_001", "node_002", "node_003"}),
        actual_elements   = elements,
        conflict_count    = conflict_count,
        split_brain_nodes = recovered,
        latency_ms        = round(latency_ms, 2),
        detail            = detail,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_suite(
    policy:  ConflictPolicy = ConflictPolicy.OR_SET,
    verbose: bool = False,
) -> list[ScenarioResult]:
    print(f"\n{'━'*68}")
    print(f"  Suite 12 — Concurrent Sync & CRDT Convergence")
    print(f"  Policy: {policy.value.upper()}  |  3 scenarios")
    print(f"{'━'*68}\n")

    results: list[ScenarioResult] = []

    for label, fn in [
        ("A — Concurrent adds",       run_scenario_a),
        ("B — Concurrent updates",    run_scenario_b),
        ("C — Split-brain reconnect", run_scenario_c),
    ]:
        print(f"  ▶ Scenario {label} … ", end="", flush=True)
        r = fn(policy, verbose)
        results.append(r)
        icon = "✓" if r.passed else "✗"
        print(f"{icon}  convergent={r.convergent}  "
              f"conflicts={r.conflict_count}  {r.latency_ms:.1f}ms")
        if not r.passed:
            print(f"       FAIL: {r.detail}")
        elif verbose:
            print(f"       {r.detail}")

    return results


def print_summary(results: list[ScenarioResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    W = 70
    print(f"\n{'═'*W}")
    print(f"  Suite 12 RESULTS  {passed}/{len(results)} passed")
    print(f"{'─'*W}")
    for r in results:
        icon  = "✓" if r.passed else "✗"
        print(f"  {icon} {r.name:<30}  {r.detail[:40]:<40}")
    print(f"{'─'*W}")
    print(f"  OR-Set convergence guarantees verified" if passed == len(results)
          else f"  {len(results)-passed} scenario(s) failed — review CRDT policy")
    print(f"{'═'*W}\n")


def save_results(results: list[ScenarioResult], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "suite":          "suite_12_concurrent_sync",
        "version":        "1.0",
        "run_at":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy":         results[0].policy if results else "unknown",
        "total":          len(results),
        "passed":         sum(1 for r in results if r.passed),
        "convergence_rate": round(
            sum(1 for r in results if r.convergent) / max(len(results), 1), 4
        ),
        "results":        [asdict(r) for r in results],
    }
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  Suite 12 results → {out}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Suite 12: Concurrent sync & CRDT convergence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--policy", choices=["lww", "or_set", "manual"],
                   default="or_set",
                   help="Conflict resolution policy to test (default: or_set)")
    p.add_argument("--out",     default=str(OUT_DEFAULT), metavar="PATH")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args    = _parse_args()
    policy  = ConflictPolicy(args.policy)
    results = run_suite(policy=policy, verbose=args.verbose)
    print_summary(results)
    save_results(results, Path(args.out))
    sys.exit(0 if all(r.passed for r in results) else 1)
