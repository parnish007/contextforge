"""
benchmark/omega_iter3.py
══════════════════════════════════════════════════════════════════════════
OMEGA Benchmark — Iteration 3 (GC OPTIMISATION + TOKEN BUDGET)

Changes from Iteration 2:
  - Historian GC threshold  : 0.60 → 0.55  (archives more duplicate nodes,
                              keeps L2 index lean, reduces retrieval payload)
  - L2 token budget cap     : 2 000 → 1 500 tokens  (hard truncation before
                              retrieval context is assembled into LLM prompt)
  - Injection patterns      : inherited from iter2 (14 patterns, unchanged)
  - Semantic threshold      : 0.80 (unchanged)

Source patches applied before this run:
  src/agents/historian/historian_agent.py:
    duplicate_threshold default: 0.60 → 0.55
  src/core/omega_config.py:
    gc_threshold default: 0.60 → 0.55
    token_budget_l2 default: 2000 → 1500

Expected improvements over Iteration 2:
  - CTO : ~276K → ~251K  (-9%)  — tighter GC + token cap reduces payload
  - CSS : ~0.756 → ~0.771       — leaner L2 index improves retrieval precision
  - ABR : 100% maintained        — no regression in security defense
  - L0  : slight decrease       — leaner index means fewer cold misses

RUN:
    python benchmark/omega_iter3.py

Output: benchmark/OMEGA_iter3_<timestamp>.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmark.live_benchmark_omega import OmegaConfig, OmegaEngine
from benchmark.omega_iter2 import _INJECTION_PATTERNS_V1  # carry forward

ITER3_CONFIG = OmegaConfig(
    iteration=3,
    description="GC Optimisation — gc_threshold 0.60→0.55, L2 budget 2000→1500",
    semantic_threshold=0.80,
    gc_threshold=0.55,                       # KEY CHANGE: tighter GC
    injection_patterns=_INJECTION_PATTERNS_V1,
    token_budget_l2=1500,                    # KEY CHANGE: token cap
    inter_turn_delay=5.0,
    live_llm=False,
    model="models/gemini-2.5-flash",
    random_seed=42,
    gc_every_n_turns=10,
    noise_tolerance=0.0,
)


def _print_pre_run():
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║  OMEGA-75  Iteration 3 — GC OPTIMISATION + TOKEN BUDGET              ║
║  Leaner knowledge graph. CTO target: < 255K tokens.                  ║
╚══════════════════════════════════════════════════════════════════════╝

Delta from Iter 2:
  gc_threshold   : 0.60 → 0.55  (archive duplicates with lower similarity)
  token_budget_l2: 2000 → 1500  (hard truncation of L2 retrieval context)

Why these changes?
  Iter 2 showed CTO=276K. The Historian GC was leaving nodes with 60-64%
  Jaccard similarity both active, causing L2 BM25 to retrieve redundant
  context and inflate token counts by ~8% per turn.

  The 1500-token budget cap ensures the assembled context never exceeds
  the model's optimal prompt window for fast inference at 15 RPM.

Injection patterns: 14 (carried from iter2, unchanged)
""")


def critique_iter3(report) -> str:
    issues = []

    # Token reduction check
    iter2_cto_estimate = 276_840
    reduction = (iter2_cto_estimate - report.cto_tokens) / iter2_cto_estimate * 100
    if reduction < 5:
        issues.append(
            f"CTO reduction only {reduction:.1f}%. "
            f"GC threshold at {ITER3_CONFIG.gc_threshold} may still be too permissive. "
            f"Consider 0.50 in iter4, or combine with L3 pruning."
        )
    else:
        issues.append(
            f"CTO reduced by {reduction:.1f}% (target >8%). Threshold tuning effective."
        )

    if report.noisy_css_mean < 0.71:
        issues.append(
            f"Noisy CSS still at {report.noisy_css_mean:.4f}. "
            f"Noisy turns account for {11/75*100:.0f}% of corpus. "
            f"Fix in iter4: add noise_tolerance=0.06 to relax threshold on noisy turns."
        )

    if report.abr_pct < 100:
        issues.append(f"ABR regression to {report.abr_pct}% — check pattern list coverage.")

    if not issues:
        issues.append("Token efficiency improved. Remaining gap: noisy-turn CSS. Address in iter4.")

    return "\n".join(f"  [{i+1}] {s}" for i, s in enumerate(issues))


if __name__ == "__main__":
    _print_pre_run()

    engine = OmegaEngine(ITER3_CONFIG)
    report = engine.run()
    engine.print_summary(report)
    engine.print_continuity_block(report)
    engine.save_report(report)

    print("\n  CRITIQUE (informs Iteration 4 patches):")
    print(critique_iter3(report))
    print(f"\n  → Next: add noise_tolerance=0.06 + rate-limit backoff, then run omega_iter4.py")
