# RATIONALE: Suite 15b — rerun update-accuracy sub-benchmark with recency weighting fix.
"""
suite_15b_rerun.py — Targeted rerun of Dataset B (update/conflict) only.

Verifies that the recency weighting fix applied to ContextForgeV3.retrieve()
improves update_accuracy from 0.229 (v1, pure BM25) to >= 0.600.

Outputs
───────
  benchmark/benchmark_memory/results/suite_15b_updated_results.json
  Prints before/after comparison table.

Usage
─────
  python -X utf8 benchmark/benchmark_memory/suite_15b_rerun.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATASETS_DIR = ROOT / "benchmark" / "benchmark_memory" / "datasets"
RESULTS_DIR  = ROOT / "benchmark" / "benchmark_memory" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

from benchmark.benchmark_memory.scripts.memory_agent import (  # noqa: E402
    StatelessRAG, MemGPT, LangGraph, ClaudeMem, HardenedRAG, ContextForgeV3,
)


def _load(filename: str) -> list[dict]:
    path = DATASETS_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["samples"] if isinstance(data, dict) and "samples" in data else data


def _keywords_hit(retrieved: list[str], keywords: list[str]) -> bool:
    joined = " ".join(retrieved).lower()
    return any(kw.lower() in joined for kw in keywords)


def eval_update_accuracy(system, samples: list[dict]) -> float:
    correct = 0
    for s in samples:
        system.reset()
        for mem in s["initial_writes"]:
            system.write(mem)
        for mem in s["update_writes"]:
            system.write(mem)
        retrieved = system.retrieve(s["query"], k=3)
        if bool(retrieved) and _keywords_hit(retrieved[:1], s["expected_keywords"]):
            correct += 1
    return round(correct / len(samples), 4) if samples else 0.0


# ── V1 baselines (pure BM25, no recency weighting) ─────────────────────────
V1_RESULTS = {
    "StatelessRAG": 0.0,
    "MemGPT":       0.4286,
    "LangGraph":    0.2286,
    "ClaudeMem":    0.4286,
    "HardenedRAG":  0.2286,
    "ContextForge": 0.2286,
}

SYSTEMS = [
    StatelessRAG,
    MemGPT,
    LangGraph,
    ClaudeMem,
    HardenedRAG,
    ContextForgeV3,
]


def main() -> None:
    ds_b = _load("dataset_b_update_conflict.json")
    n = len(ds_b)

    print(f"\n{'='*65}")
    print(f"  Suite 15b — Update Accuracy Re-run (recency weighting fix)")
    print(f"  Dataset B: {n} samples | Measuring: update_accuracy (top-1)")
    print(f"{'='*65}")

    results = {}
    t0_total = time.perf_counter()

    for Cls in SYSTEMS:
        sys_name = Cls.name if hasattr(Cls, "name") else Cls.__name__
        try:
            system = Cls()
        except ImportError as e:
            print(f"  {sys_name:<16} SKIP — {e}")
            continue

        t0  = time.perf_counter()
        acc = eval_update_accuracy(system, ds_b)
        elapsed = round(time.perf_counter() - t0, 3)

        v1  = V1_RESULTS.get(sys_name, 0.0)
        delta = acc - v1
        flag = " ✓" if acc >= 0.600 else (" ~" if acc >= 0.400 else "  ")

        print(f"  {sys_name:<16}  v1={v1:.3f}  v2={acc:.3f}  Δ={delta:+.3f}{flag}  ({elapsed}s)")
        results[sys_name] = {
            "update_accuracy_v1": v1,
            "update_accuracy_v2": acc,
            "delta_pp": round(delta * 100, 2),
        }

    total_elapsed = round(time.perf_counter() - t0_total, 3)

    cf  = results.get("ContextForge", {})
    acc = cf.get("update_accuracy_v2", 0.0)
    target_met = acc >= 0.600

    print(f"\n{'='*65}")
    print(f"  ContextForge update_accuracy: {cf.get('update_accuracy_v1',0):.3f} → {acc:.3f}")
    print(f"  Target (≥0.600): {'MET ✓' if target_met else 'NOT MET ✗'}")
    print(f"  Total elapsed: {total_elapsed}s")
    print(f"{'='*65}\n")

    out = {
        "suite":             "suite_15b_update_accuracy_rerun",
        "recency_fix":       "recency_bias=0.65 (position-based, ContextForge only)",
        "n_samples":         n,
        "target_accuracy":   0.600,
        "target_met":        target_met,
        "elapsed_total_s":   total_elapsed,
        "systems":           results,
        "delta_vs_v1": {
            "update_accuracy_v1": V1_RESULTS["ContextForge"],
            "update_accuracy_v2": acc,
            "improvement_pp":    round((acc - V1_RESULTS["ContextForge"]) * 100, 2),
        },
    }
    out_path = RESULTS_DIR / "suite_15b_updated_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"  Results → {out_path.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
