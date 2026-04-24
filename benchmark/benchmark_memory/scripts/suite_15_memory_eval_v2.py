# RATIONALE: Suite 15 v2 — full memory quality evaluation with recency weighting fix.
"""
suite_15_memory_eval_v2.py — Full Suite 15 evaluation, recency fix applied.

Differences from v1:
  - ContextForge retrieval uses recency_bias=0.75 (position-based)
  - Output goes to results/suite_15_final_report_v2.json with the exact
    schema required for paper figures and comparisons.
  - Includes delta_vs_v1_report section.

Usage:
    python -X utf8 benchmark/benchmark_memory/scripts/suite_15_memory_eval_v2.py
    python -X utf8 benchmark/benchmark_memory/scripts/suite_15_memory_eval_v2.py --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DATASETS_DIR = ROOT / "benchmark" / "benchmark_memory" / "datasets"
RESULTS_DIR  = ROOT / "benchmark" / "benchmark_memory" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

from benchmark.benchmark_memory.scripts.memory_agent import (  # noqa: E402
    StatelessRAG, MemGPT, LangGraph, ClaudeMem, HardenedRAG, ContextForgeV3,
)

# ── v1 baselines for delta computation ───────────────────────────────────────
V1_CF = {
    "recall_at_3":          0.9667,
    "precision_at_3":       0.4278,
    "f1":                   0.57,
    "exact_match":          0.9,
    "update_accuracy":      0.2286,
    "delete_accuracy":      1.0,
    "poison_resistance":    0.7714,
    "memory_integrity_score": 0.7417,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load(filename: str) -> list[dict]:
    path = DATASETS_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["samples"] if isinstance(data, dict) and "samples" in data else data


def _keywords_hit(retrieved: list[str], keywords: list[str]) -> bool:
    joined = " ".join(retrieved).lower()
    return any(kw.lower() in joined for kw in keywords)


def _keywords_absent(retrieved: list[str], keywords: list[str]) -> bool:
    joined = " ".join(retrieved).lower()
    return not any(kw.lower() in joined for kw in keywords)


def _precision_at_k(retrieved: list[str], expected_keywords: list[str]) -> float:
    if not retrieved:
        return 0.0
    hits = sum(1 for r in retrieved if any(kw.lower() in r.lower() for kw in expected_keywords))
    return hits / len(retrieved)


def _recall_at_k(retrieved: list[str], memories: list[str], correct_idx: int,
                 expected_keywords: list[str]) -> float:
    correct_text = memories[correct_idx] if correct_idx < len(memories) else ""
    if correct_text in retrieved:
        return 1.0
    return 1.0 if _keywords_hit(retrieved, expected_keywords) else 0.0


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ── Dataset evaluators ────────────────────────────────────────────────────────

def eval_dataset_a(system, samples: list[dict], verbose: bool) -> dict:
    recalls, precisions, f1s, exact_matches = [], [], [], []
    for s in samples:
        system.reset()
        for mem in s["store"]:
            system.write(mem)
        retrieved = system.retrieve(s["query"], k=3)
        rec  = _recall_at_k(retrieved, s["store"], s["correct_idx"], s["expected_keywords"])
        prec = _precision_at_k(retrieved, s["expected_keywords"])
        f    = _f1(prec, rec)
        exact = 1 if (s["correct_idx"] < len(s["store"]) and s["store"][s["correct_idx"]] in retrieved) else 0
        recalls.append(rec); precisions.append(prec); f1s.append(f); exact_matches.append(exact)
        if verbose:
            print(f"  [{s['id']}] {'HIT' if rec else 'MISS'}  q={s['query'][:50]!r}")
    n = len(samples)
    return {
        "recall_at_3":    round(sum(recalls)      / n, 4),
        "precision_at_3": round(sum(precisions)   / n, 4),
        "f1":             round(sum(f1s)           / n, 4),
        "exact_match":    round(sum(exact_matches) / n, 4),
        "n_samples":      n,
    }


def eval_dataset_b(system, samples: list[dict], verbose: bool) -> dict:
    correct = 0
    for s in samples:
        system.reset()
        for mem in s["initial_writes"]:
            system.write(mem)
        for mem in s["update_writes"]:
            system.write(mem)
        retrieved  = system.retrieve(s["query"], k=3)
        fresh_top1 = bool(retrieved) and _keywords_hit(retrieved[:1], s["expected_keywords"])
        if fresh_top1:
            correct += 1
        if verbose:
            top1 = retrieved[0][:60] if retrieved else "(empty)"
            print(f"  [{s['id']}] {'OK' if fresh_top1 else '--'}  top1={top1!r}")
    n = len(samples)
    return {"update_accuracy": round(correct / n, 4), "n_samples": n}


def eval_dataset_c(system, samples: list[dict], verbose: bool) -> dict:
    correct = 0
    for s in samples:
        system.reset()
        for mem in s["writes"]:
            system.write(mem)
        for target in s["delete_targets"]:
            system.delete(target)
        retrieved      = system.retrieve(s["query"], k=3)
        deleted_absent = _keywords_absent(retrieved, s["deleted_keywords"])
        survivors_ok   = True
        if s.get("expected_survivors"):
            survivors_ok = _keywords_hit(retrieved, s["expected_survivors"])
        if s.get("expected_empty", False):
            ok = len(retrieved) == 0 or deleted_absent
        else:
            ok = deleted_absent and survivors_ok
        if ok:
            correct += 1
        if verbose:
            print(f"  [{s['id']}] {'OK' if ok else '--'}  del_absent={deleted_absent}")
    n = len(samples)
    return {"delete_accuracy": round(correct / n, 4), "n_samples": n}


def eval_dataset_d(system, samples: list[dict], verbose: bool) -> dict:
    correct = 0
    n_blocked = 0
    for s in samples:
        system.reset()
        for mem in s["benign_writes"]:
            system.write(mem)
        any_blocked = False
        for mem in s["adversarial_writes"]:
            result = system.write(mem)
            if not result.accepted:
                any_blocked = True
                n_blocked += 1
        retrieved     = system.retrieve(s["query"], k=3)
        poison_absent = _keywords_absent(retrieved, s["poison_keywords"])
        ok = any_blocked or poison_absent
        if ok:
            correct += 1
        if verbose:
            print(f"  [{s['id']}] {'OK' if ok else '--'}  blocked={any_blocked}  poison_absent={poison_absent}")
    n = len(samples)
    return {"poison_resistance": round(correct / n, 4), "n_blocked": n_blocked, "n_samples": n}


def _mis(a: dict, b: dict, c: dict, d: dict) -> float:
    return round(
        (a["recall_at_3"] + b["update_accuracy"] + c["delete_accuracy"] + d["poison_resistance"])
        / 4, 4
    )


def _flatten8(a: dict, b: dict, c: dict, d: dict, mis: float) -> dict:
    """Return the 8-metric flat dict required by the v2 report schema."""
    return {
        "retrieval_recall_at_3":    a["recall_at_3"],
        "retrieval_precision_at_3": a["precision_at_3"],
        "retrieval_f1":             a["f1"],
        "exact_match":              a["exact_match"],
        "update_accuracy":          b["update_accuracy"],
        "delete_accuracy":          c["delete_accuracy"],
        "poison_resistance":        d["poison_resistance"],
        "memory_integrity_score":   mis,
    }


# ── Systems ───────────────────────────────────────────────────────────────────

SYSTEMS = [StatelessRAG, MemGPT, LangGraph, ClaudeMem, HardenedRAG, ContextForgeV3]

SYSTEM_ALIASES = {
    "ContextForge": "ContextForge_v3",
}


def run_suite(verbose: bool = False) -> dict:
    ds_a = _load("dataset_a_ground_truth.json")
    ds_b = _load("dataset_b_update_conflict.json")
    ds_c = _load("dataset_c_delete_forget.json")
    ds_d = _load("dataset_d_poisoning.json")

    print(f"\n{'='*70}")
    print(f"  Suite 15 v2 — Memory Quality Benchmark (recency weighting active)")
    print(f"  Datasets: A={len(ds_a)} B={len(ds_b)} C={len(ds_c)} D={len(ds_d)}")
    print(f"  Systems : {len(SYSTEMS)}")
    print(f"{'='*70}")

    systems_comparison = {}
    no_filter_row = None
    with_guard_row = None

    t0_total = time.perf_counter()

    for Cls in SYSTEMS:
        sys_name = Cls.name if hasattr(Cls, "name") else Cls.__name__
        print(f"\n  >> {sys_name}")
        try:
            system = Cls()
        except ImportError as e:
            print(f"     SKIP — {e}")
            continue

        t0    = time.perf_counter()
        res_a = eval_dataset_a(system, ds_a, verbose)
        res_b = eval_dataset_b(system, ds_b, verbose)
        res_c = eval_dataset_c(system, ds_c, verbose)
        res_d = eval_dataset_d(system, ds_d, verbose)
        elapsed = round(time.perf_counter() - t0, 3)
        mis = _mis(res_a, res_b, res_c, res_d)

        print(
            f"     Recall@3={res_a['recall_at_3']:.3f}  "
            f"Update={res_b['update_accuracy']:.3f}  "
            f"Delete={res_c['delete_accuracy']:.3f}  "
            f"Poison={res_d['poison_resistance']:.3f}  "
            f"MIS={mis:.3f}  ({elapsed}s)"
        )

        flat = _flatten8(res_a, res_b, res_c, res_d, mis)
        alias = SYSTEM_ALIASES.get(sys_name, sys_name)
        systems_comparison[alias] = flat

        if sys_name in ("LangGraph", "StatelessRAG", "MemGPT"):
            no_filter_row = flat  # use LangGraph as "no security filter" representative
        if sys_name == "ContextForge":
            with_guard_row = flat

    total_elapsed = round(time.perf_counter() - t0_total, 3)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  {'System':<18} {'R@3':>6} {'Upd':>6} {'Del':>6} {'Poi':>6} {'MIS':>6}")
    print(f"  {'-'*18} {'-----':>6} {'-----':>6} {'-----':>6} {'-----':>6} {'-----':>6}")
    for name, row in systems_comparison.items():
        print(
            f"  {name:<18} "
            f"{row['retrieval_recall_at_3']:>6.3f} "
            f"{row['update_accuracy']:>6.3f} "
            f"{row['delete_accuracy']:>6.3f} "
            f"{row['poison_resistance']:>6.3f} "
            f"{row['memory_integrity_score']:>6.3f}"
        )
    print(f"{'='*70}\n")

    # ── Build v2 report ───────────────────────────────────────────────────────
    cf_v2 = systems_comparison.get("ContextForge_v3", {})
    ua_v2 = cf_v2.get("update_accuracy", 0.0)

    no_filter = no_filter_row or systems_comparison.get("LangGraph", {})
    with_guard = with_guard_row or cf_v2

    report = {
        "suite":                "suite_15_memory_benchmark_v2",
        "recency_fix_applied":  True,
        "recency_config":       {"bias": 0.75, "type": "position_based", "system": "ContextForge_v3"},
        "run_at":               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_samples":            {"A": len(ds_a), "B": len(ds_b), "C": len(ds_c), "D": len(ds_d)},
        "elapsed_total_s":      total_elapsed,
        "no_filter":            no_filter,
        "with_guard":           with_guard,
        "systems_comparison":   systems_comparison,
        "delta_vs_v1_report":   {
            "update_accuracy_before": V1_CF["update_accuracy"],
            "update_accuracy_after":  ua_v2,
            "improvement_pp":         round((ua_v2 - V1_CF["update_accuracy"]) * 100, 2),
            "mis_before":             V1_CF["memory_integrity_score"],
            "mis_after":              cf_v2.get("memory_integrity_score", 0.0),
            "mis_improvement_pp":     round(
                (cf_v2.get("memory_integrity_score", 0.0) - V1_CF["memory_integrity_score"]) * 100, 2
            ),
        },
    }

    out_path = RESULTS_DIR / "suite_15_final_report_v2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"  Report saved → {out_path.relative_to(ROOT)}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Suite 15 v2 — Memory Quality Benchmark")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    run_suite(verbose=args.verbose)
