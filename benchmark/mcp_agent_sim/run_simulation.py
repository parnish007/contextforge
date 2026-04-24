# RATIONALE: Main runner for the MCP coding-agent simulation.
#            Executes all 8 scenarios, prints pass/fail with step detail,
#            prints a per-tool call summary, and exits non-zero on any failure.
"""
run_simulation.py — ContextForge MCP agent simulation runner.

Usage:
    python -m benchmark.mcp_agent_sim.run_simulation
    python -X utf8 benchmark/mcp_agent_sim/run_simulation.py

Exit code 0 = all scenarios pass, 1 = one or more scenarios fail.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure project root is on sys.path when run as a script
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from benchmark.mcp_agent_sim.coding_agent_sim import CodingAgentSim
from benchmark.mcp_agent_sim.mcp_tool_client import MCPToolClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEP = "─" * 70
_DSEP = "═" * 70


def _header(text: str) -> None:
    print(f"\n{_DSEP}")
    print(f"  {text}")
    print(_DSEP)


def _section(text: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {text}")
    print(_SEP)


# ---------------------------------------------------------------------------
# Tool coverage check — ensures every known MCP tool was exercised
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "init_project",
    "capture_decision",
    "get_knowledge_node",
    "load_context",
    "search_context",
    "update_decision",
    "delete_decision",
    "rollback",
    "snapshot",
    "list_events",
    "replay_sync",
    "agent_status",
}


def check_tool_coverage(all_call_logs: list[dict]) -> tuple[set[str], set[str]]:
    """Return (covered, missing) tool name sets."""
    covered = {c["tool"] for c in all_call_logs}
    missing = EXPECTED_TOOLS - covered
    return covered, missing


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    _header("ContextForge MCP Coding-Agent Simulation")
    print(f"  Simulates 8 real-world development scenarios across 12 MCP tools.")
    print(f"  Each scenario uses MCPToolClient (in-process, no network required).")

    sim = CodingAgentSim()
    t_start = time.perf_counter()

    _section("Running 8 scenarios…")
    results = sim.run_all()

    # ------------------------------------------------------------------ #
    # Print detailed results
    # ------------------------------------------------------------------ #
    _section("Scenario Results")
    passed = 0
    failed = 0
    for res in results:
        print(str(res))
        if res.passed:
            passed += 1
        else:
            failed += 1

    # ------------------------------------------------------------------ #
    # Tool-level call summary — reuse the already-completed sim's call log
    # (reset() preserves _call_log across scenarios so we get full coverage)
    # ------------------------------------------------------------------ #
    _section("MCP Tool Coverage & Call Statistics")

    summary = sim.client.summary()
    all_calls = sim.client._call_log
    covered, missing = check_tool_coverage(all_calls)

    print(f"\n  Total MCP tool calls : {summary['total_calls']}")
    print(f"  Blocked by ReviewerGuard: {summary['blocked_calls']}")
    print(f"  Average call latency : {summary['avg_latency_ms']:.3f} ms (in-process)")
    print(f"  Tools exercised      : {len(covered)}/{len(EXPECTED_TOOLS)}")

    print("\n  Per-tool breakdown:")
    tool_counts = summary["tool_counts"]
    tool_blocks = summary.get("tool_blocks", {})
    for tool in sorted(EXPECTED_TOOLS):
        count = tool_counts.get(tool, 0)
        blocks = tool_blocks.get(tool, 0)
        status = "✓" if tool in covered else "✗ MISSING"
        block_note = f"  [{blocks} blocked]" if blocks else ""
        print(f"    {status:8s}  {tool:<25s} {count:>4d} calls{block_note}")

    if missing:
        print(f"\n  ⚠ Not yet exercised: {', '.join(sorted(missing))}")
    else:
        print(f"\n  ✓ All {len(EXPECTED_TOOLS)} MCP tools exercised in simulation.")

    # ------------------------------------------------------------------ #
    # Final summary
    # ------------------------------------------------------------------ #
    total_ms = (time.perf_counter() - t_start) * 1000
    _header(f"Summary  —  {passed}/{len(results)} scenarios passed  |  {total_ms:.0f} ms total")

    if failed == 0:
        print("  ALL SCENARIOS PASSED ✓")
        print()
        return 0
    else:
        print(f"  {failed} SCENARIO(S) FAILED ✗")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
