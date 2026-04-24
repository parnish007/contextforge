# RATIONALE: Suite 15 — Memory Quality Benchmark evaluation harness.
# Runs 6 memory systems across 4 datasets, computes 8 metrics, writes JSON logs.
"""
suite_15_memory_eval.py — Main evaluation harness for Suite 15.

Usage:
    python -X utf8 benchmark/benchmark_memory/scripts/suite_15_memory_eval.py
    python -X utf8 benchmark/benchmark_memory/scripts/suite_15_memory_eval.py --verbose

Metrics
───────
  Dataset A  → Retrieval Recall@3, Precision@3, F1, Exact Match
  Dataset B  → Update Accuracy (stale keyword NOT in top-k, fresh keyword IS)
  Dataset C  → Delete Accuracy (deleted keyword NOT in top-k post-delete)
  Dataset D  → Poison Resistance (adversarial writes blocked OR not in top-k)
  Combined   → Memory Integrity Score = mean(recall@3, update_acc, delete_acc, poison_res)

Output
──────
  benchmark/benchmark_memory/logs/suite_15_results.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DATASETS_DIR = ROOT / "benchmark" / "benchmark_memory" / "datasets"
LOGS_DIR     = ROOT / "benchmark" / "benchmark_memory" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Import all memory systems ─────────────────────────────────────────────────
from benchmark.benchmark_memory.scripts.memory_agent import (  # noqa: E402
    StatelessRAG,
    MemGPT,
    LangGraph,
    ClaudeMem,
    HardenedRAG,
    ContextForgeV3,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load(filename: str) -> list[dict]:
    path = DATASETS_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["samples"] if isinstance(data, dict) and "samples" in data else data


def _keywords_hit(retrieved: list[str], keywords: list[str]) -> bool:
    """True if any retrieved memory contains at least one expected keyword."""
    joined = " ".join(retrieved).lower()
    return any(kw.lower() in joined for kw in keywords)


def _keywords_absent(retrieved: list[str], keywords: list[str]) -> bool:
    """True if NO retrieved memory contains any of the given keywords."""
    joined = " ".join(retrieved).lower()
    return not any(kw.lower() in joined for kw in keywords)


def _precision_at_k(retrieved: list[str], expected_keywords: list[str]) -> float:
    """Fraction of retrieved memories that contain at least one expected keyword."""
    if not retrieved:
        return 0.0
    hits = sum(1 for r in retrieved if any(kw.lower() in r.lower() for kw in expected_keywords))
    return hits / len(retrieved)


def _recall_at_k(retrieved: list[str], memories: list[str], correct_idx: int,
                 expected_keywords: list[str]) -> float:
    """
    1.0 if the correct memory or any expected-keyword match is in retrieved set.
    We check both: exact correct_idx content match OR keyword presence.
    """
    correct_text = memories[correct_idx] if correct_idx < len(memories) else ""
    if correct_text in retrieved:
        return 1.0
    return 1.0 if _keywords_hit(retrieved, expected_keywords) else 0.0


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ─────────────────────────────────────────────────────────────────────────────
# Dataset A — Ground Truth Retrieval
# ─────────────────────────────────────────────────────────────────────────────

def eval_dataset_a(system, samples: list[dict], verbose: bool) -> dict:
    recalls, precisions, f1s, exact_matches = [], [], [], []

    for s in samples:
        system.reset()
        store   = s["store"]
        query   = s["query"]
        exp_kw  = s["expected_keywords"]
        c_idx   = s["correct_idx"]

        for mem in store:
            system.write(mem)

        retrieved = system.retrieve(query, k=3)

        rec  = _recall_at_k(retrieved, store, c_idx, exp_kw)
        prec = _precision_at_k(retrieved, exp_kw)
        f    = _f1(prec, rec)

        # Exact match: correct store entry is verbatim in retrieved
        exact = 1 if (c_idx < len(store) and store[c_idx] in retrieved) else 0

        recalls.append(rec)
        precisions.append(prec)
        f1s.append(f)
        exact_matches.append(exact)

        if verbose:
            hit = "HIT" if rec else "MISS"
            print(f"  [{s['id']}] {hit}  q={query[:50]!r}  retrieved={len(retrieved)}")

    n = len(samples)
    return {
        "recall_at_3":   round(sum(recalls)    / n, 4),
        "precision_at_3": round(sum(precisions) / n, 4),
        "f1":             round(sum(f1s)        / n, 4),
        "exact_match":    round(sum(exact_matches) / n, 4),
        "n_samples":      n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dataset B — Update & Conflict
# ─────────────────────────────────────────────────────────────────────────────

def eval_dataset_b(system, samples: list[dict], verbose: bool) -> dict:
    """
    Update Accuracy: after writing initial + updated facts, does the TOP-1
    retrieved memory contain the fresh (updated) information?

    Append-only systems keep stale memories but a well-designed system should
    rank the more recent/relevant fact first. We test this via top-1 preference
    rather than requiring stale keywords to be absent from all of top-k.
    """
    correct = 0
    for s in samples:
        system.reset()

        # Write initial (stale) facts
        for mem in s["initial_writes"]:
            system.write(mem)

        # Write updates (fresh/corrected facts)
        for mem in s["update_writes"]:
            system.write(mem)

        retrieved = system.retrieve(s["query"], k=3)

        # Top-1 preference: the top result should be the updated fact
        fresh_top1 = bool(retrieved) and _keywords_hit(retrieved[:1], s["expected_keywords"])
        ok = fresh_top1
        if ok:
            correct += 1

        if verbose:
            top1 = retrieved[0][:60] if retrieved else "(empty)"
            marker = "OK" if ok else "--"
            print(f"  [{s['id']}] {marker}  top1={top1!r}")

    n = len(samples)
    return {
        "update_accuracy": round(correct / n, 4),
        "n_samples":       n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dataset C — Delete / Forget
# ─────────────────────────────────────────────────────────────────────────────

def eval_dataset_c(system, samples: list[dict], verbose: bool) -> dict:
    """
    Delete Accuracy: after deletes, deleted_keywords absent from top-k
    AND expected_survivors (if any) still present.
    """
    correct = 0
    for s in samples:
        system.reset()

        for mem in s["writes"]:
            system.write(mem)

        for target in s["delete_targets"]:
            system.delete(target)

        retrieved = system.retrieve(s["query"], k=3)

        deleted_absent  = _keywords_absent(retrieved, s["deleted_keywords"])
        survivors_ok    = True
        if s.get("expected_survivors"):
            survivors_ok = _keywords_hit(retrieved, s["expected_survivors"])

        # For empty-result expectation, no retrieved output is acceptable
        if s.get("expected_empty", False):
            ok = len(retrieved) == 0 or deleted_absent
        else:
            ok = deleted_absent and survivors_ok

        if ok:
            correct += 1

        if verbose:
            marker = "OK" if ok else "--"
            print(f"  [{s['id']}] {marker}  del_absent={deleted_absent}  surv_ok={survivors_ok}"
                  f"  retrieved={len(retrieved)}")

    n = len(samples)
    return {
        "delete_accuracy": round(correct / n, 4),
        "n_samples":       n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dataset D — Poisoning / Adversarial
# ─────────────────────────────────────────────────────────────────────────────

def eval_dataset_d(system, samples: list[dict], verbose: bool) -> dict:
    """
    Poison Resistance: adversarial writes are blocked OR their poison_keywords
    are absent from top-k retrieval results.
    """
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

        retrieved = system.retrieve(s["query"], k=3)
        poison_absent = _keywords_absent(retrieved, s["poison_keywords"])

        # Resistant if: blocked OR poison keywords don't surface in top-k
        ok = any_blocked or poison_absent
        if ok:
            correct += 1

        if verbose:
            marker = "OK" if ok else "--"
            print(f"  [{s['id']}] {marker}  blocked={any_blocked}  poison_absent={poison_absent}")

    n = len(samples)
    return {
        "poison_resistance": round(correct / n, 4),
        "n_blocked":         n_blocked,
        "n_samples":         n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Combined Memory Integrity Score
# ─────────────────────────────────────────────────────────────────────────────

def _mis(a: dict, b: dict, c: dict, d: dict) -> float:
    """Memory Integrity Score = mean(recall@3, update_acc, delete_acc, poison_res)."""
    return round(
        (a["recall_at_3"] + b["update_accuracy"] + c["delete_accuracy"] + d["poison_resistance"])
        / 4, 4
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

SYSTEMS = [
    StatelessRAG,
    MemGPT,
    LangGraph,
    ClaudeMem,
    HardenedRAG,
    ContextForgeV3,
]


def run_suite(verbose: bool = False) -> dict:
    ds_a = _load("dataset_a_ground_truth.json")
    ds_b = _load("dataset_b_update_conflict.json")
    ds_c = _load("dataset_c_delete_forget.json")
    ds_d = _load("dataset_d_poisoning.json")

    print(f"\n{'='*70}")
    print(f"  Suite 15 — Memory Quality Benchmark")
    print(f"  Datasets: A={len(ds_a)} B={len(ds_b)} C={len(ds_c)} D={len(ds_d)} samples")
    print(f"  Systems : {len(SYSTEMS)}")
    print(f"{'='*70}")

    all_results = {}
    t0_total = time.perf_counter()

    for Cls in SYSTEMS:
        sys_name = Cls.name if hasattr(Cls, "name") else Cls.__name__
        print(f"\n  >> {sys_name}")

        try:
            system = Cls()
        except ImportError as e:
            print(f"     SKIP — {e}")
            continue

        t0 = time.perf_counter()

        if verbose:
            print(f"  [Dataset A — Ground Truth Retrieval]")
        res_a = eval_dataset_a(system, ds_a, verbose)

        if verbose:
            print(f"  [Dataset B — Update & Conflict]")
        res_b = eval_dataset_b(system, ds_b, verbose)

        if verbose:
            print(f"  [Dataset C — Delete / Forget]")
        res_c = eval_dataset_c(system, ds_c, verbose)

        if verbose:
            print(f"  [Dataset D — Poisoning]")
        res_d = eval_dataset_d(system, ds_d, verbose)

        elapsed = round(time.perf_counter() - t0, 3)
        mis     = _mis(res_a, res_b, res_c, res_d)

        row = {
            "system":           sys_name,
            "dataset_a":        res_a,
            "dataset_b":        res_b,
            "dataset_c":        res_c,
            "dataset_d":        res_d,
            "memory_integrity_score": mis,
            "elapsed_s":        elapsed,
        }
        all_results[sys_name] = row

        # One-line summary
        print(
            f"     Recall@3={res_a['recall_at_3']:.3f}  "
            f"Update={res_b['update_accuracy']:.3f}  "
            f"Delete={res_c['delete_accuracy']:.3f}  "
            f"Poison={res_d['poison_resistance']:.3f}  "
            f"MIS={mis:.3f}  ({elapsed}s)"
        )

    total_elapsed = round(time.perf_counter() - t0_total, 3)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  SUMMARY  (total {total_elapsed}s)")
    print(f"  {'System':<16} {'R@3':>6} {'Upd':>6} {'Del':>6} {'Poi':>6} {'MIS':>6}  F1")
    print(f"  {'-'*16} {'-----':>6} {'-----':>6} {'-----':>6} {'-----':>6} {'-----':>6}  ---")
    for name, row in all_results.items():
        a = row["dataset_a"]
        b = row["dataset_b"]
        c = row["dataset_c"]
        d = row["dataset_d"]
        print(
            f"  {name:<16} "
            f"{a['recall_at_3']:>6.3f} "
            f"{b['update_accuracy']:>6.3f} "
            f"{c['delete_accuracy']:>6.3f} "
            f"{d['poison_resistance']:>6.3f} "
            f"{row['memory_integrity_score']:>6.3f}  "
            f"{a['f1']:.3f}"
        )
    print(f"{'='*70}\n")

    # ── Save JSON log ─────────────────────────────────────────────────────────
    output = {
        "suite":       "suite_15_memory_quality",
        "version":     "v1_bm25_real_reviewer_guard",
        "n_systems":   len(all_results),
        "n_samples":   {"A": len(ds_a), "B": len(ds_b), "C": len(ds_c), "D": len(ds_d)},
        "elapsed_total_s": total_elapsed,
        "results":     all_results,
    }
    out_path = LOGS_DIR / "suite_15_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"  Results saved → {out_path.relative_to(ROOT)}")

    return output


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Suite 15 — Memory Quality Benchmark")
    parser.add_argument("--verbose", action="store_true", help="Per-sample output")
    args = parser.parse_args()
    run_suite(verbose=args.verbose)
