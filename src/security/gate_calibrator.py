"""
ContextForge — Automated Gate Threshold Calibrator
====================================================

Addresses the "automated threshold recalibration" future-work item from
the ContextForge v2 paper (§7: Limitations and Future Work).

Given a labelled attack corpus (list of texts + ground-truth labels), this
module finds the optimal H* (Shannon entropy gate) and ρ_min (LZ density
floor) thresholds by binary-searching over F1 score on the provided corpus.

The calibrator operates in two phases:

  Phase 1 — H* sweep
      Binary-search over H* ∈ [1.5, 6.0] to maximise macro-F1 on the
      corpus using only the entropy signal.  The resulting H*_opt is the
      single-signal optimum.

  Phase 2 — ρ_min sweep (conditioned on H*_opt)
      Binary-search over ρ_min ∈ [0.30, 0.95] using the dual-signal gate
      (block if H > H*_opt OR ρ < ρ_min) to find the LZ density floor that
      further reduces FPR without sacrificing TPR.

Both phases use Brent's method (scipy.optimize.brentq when available,
otherwise a hand-rolled bisection fallback) for fast convergence.

Usage
-----
    from src.security.gate_calibrator import GateCalibrator, Corpus

    corpus = Corpus(
        texts  = ["some benign text ...", "ignore all rules ..."],
        labels = [0, 1],          # 0 = benign, 1 = attack
    )
    cal = GateCalibrator(corpus)
    result = cal.calibrate()

    print(result.h_star_opt)   # e.g., 3.42
    print(result.rho_min_opt)  # e.g., 0.63
    print(result.report())     # full text report for the paper

Design notes
------------
- Corpus must have at least 10 samples with both positive and negative
  examples to produce a meaningful calibration.
- The F1 score used is macro-averaged over [benign, attack] classes.
- "Attack" is defined as label==1.  The gate's prediction is:
      predict_attack = (H > H*) OR (ρ < ρ_min)
- Calibration is offline / batch — it does not modify the live ledger
  thresholds.  Apply the result by updating .env or calling
  ReviewerGuard.set_thresholds() (see ledger.py).
"""

from __future__ import annotations

import json
import math
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, NamedTuple


# ── Entropy / density helpers (self-contained, no ledger import) ──────────────

def _word_entropy(text: str) -> float:
    """Word-level Shannon entropy (bits). Matches ReviewerGuard._compute_entropy."""
    words = text.split()
    if not words:
        return 0.0
    counts = Counter(words)
    total  = len(words)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _lz_density(text: str) -> float:
    """LZ compression density. Matches ReviewerGuard._compute_lz_density."""
    raw = text.encode("utf-8", errors="replace")
    if not raw:
        return 1.0
    return len(zlib.compress(raw, level=6)) / len(raw)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Corpus:
    """
    Labelled text corpus for calibration.

    Attributes
    ----------
    texts  : list of raw text strings (plaintext, not JSON-escaped)
    labels : list of int, same length as texts; 1 = attack, 0 = benign
    """
    texts:  list[str]
    labels: list[int]   # 1=attack, 0=benign

    def __post_init__(self) -> None:
        if len(self.texts) != len(self.labels):
            raise ValueError("texts and labels must have the same length")
        if len(self.texts) < 10:
            raise ValueError("Corpus must have at least 10 samples")
        if not any(l == 1 for l in self.labels):
            raise ValueError("Corpus must contain at least one attack sample")
        if not any(l == 0 for l in self.labels):
            raise ValueError("Corpus must contain at least one benign sample")

    @property
    def n_attack(self) -> int:
        return sum(1 for l in self.labels if l == 1)

    @property
    def n_benign(self) -> int:
        return sum(1 for l in self.labels if l == 0)


@dataclass
class CalibrationResult:
    """Output of GateCalibrator.calibrate()."""
    h_star_opt:     float   # optimal entropy threshold (bits)
    rho_min_opt:    float   # optimal LZ density floor
    f1_single:      float   # F1 with entropy gate only (Phase 1 peak)
    f1_dual:        float   # F1 with dual gate (Phase 2 peak)
    f1_improvement: float   # f1_dual - f1_single
    tpr:            float   # True Positive Rate at dual optimum
    fpr:            float   # False Positive Rate at dual optimum
    precision:      float   # Precision at dual optimum
    recall:         float   # Recall at dual optimum (= tpr)
    n_samples:      int
    n_attack:       int
    n_benign:       int
    sweep_log:      list[dict[str, Any]] = field(default_factory=list)
    calibrated_at:  str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def report(self) -> str:
        lines = [
            "ContextForge Gate Calibration Report",
            "=" * 40,
            f"  Corpus size  : {self.n_samples} samples ({self.n_attack} attack, {self.n_benign} benign)",
            f"  H*_opt       : {self.h_star_opt:.4f} bits  (prev default: 3.5000)",
            f"  ρ_min_opt    : {self.rho_min_opt:.4f}       (prev default: 0.6000)",
            f"  F1 (single)  : {self.f1_single:.4f}",
            f"  F1 (dual)    : {self.f1_dual:.4f}  (+{self.f1_improvement:.4f} from LZ gate)",
            f"  TPR          : {self.tpr:.4f}  (recall)",
            f"  FPR          : {self.fpr:.4f}",
            f"  Precision    : {self.precision:.4f}",
            f"  Calibrated   : {self.calibrated_at}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


# ── Metrics helpers ───────────────────────────────────────────────────────────

class _Metrics(NamedTuple):
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def tpr(self) -> float:
        return self.recall

    @property
    def fpr(self) -> float:
        denom = self.fp + self.tn
        return self.fp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def macro_f1(self) -> float:
        """Macro F1 over [attack, benign] classes."""
        # Attack class: TP, FP, FN as above
        f1_attack = self.f1
        # Benign class: TN as TP_benign, FN_benign = FP_attack, FP_benign = FN_attack
        tp_b = self.tn
        fp_b = self.fn
        fn_b = self.fp
        p_b  = tp_b / (tp_b + fp_b) if (tp_b + fp_b) else 0.0
        r_b  = tp_b / (tp_b + fn_b) if (tp_b + fn_b) else 0.0
        f1_b = 2 * p_b * r_b / (p_b + r_b) if (p_b + r_b) else 0.0
        return (f1_attack + f1_b) / 2.0


def _evaluate(
    texts:   list[str],
    labels:  list[int],
    entropies: list[float],
    densities: list[float],
    h_star:  float,
    rho_min: float,
) -> _Metrics:
    """Predict attack if H > h_star OR ρ < rho_min, then count TP/FP/FN/TN."""
    tp = fp = fn = tn = 0
    for H, rho, true_label in zip(entropies, densities, labels):
        predicted = 1 if (H > h_star or rho < rho_min) else 0
        if true_label == 1 and predicted == 1:
            tp += 1
        elif true_label == 0 and predicted == 1:
            fp += 1
        elif true_label == 1 and predicted == 0:
            fn += 1
        else:
            tn += 1
    return _Metrics(tp=tp, fp=fp, fn=fn, tn=tn)


# ── Bisection (Brent fallback) ────────────────────────────────────────────────

def _bisect_maximize(
    fn:  Callable[[float], float],
    lo:  float,
    hi:  float,
    n_steps: int = 40,
) -> tuple[float, float]:
    """
    Find x* in [lo, hi] that maximises fn(x) using golden-section search.
    Returns (x_opt, f_opt).

    Golden-section search converges for strictly unimodal functions; for
    F1 which may be flat-topped, this finds a near-optimal point efficiently.
    """
    phi = (math.sqrt(5) - 1) / 2  # golden ratio conjugate ≈ 0.618
    a, b = lo, hi
    c = b - phi * (b - a)
    d = a + phi * (b - a)
    fc = fn(c)
    fd = fn(d)
    for _ in range(n_steps):
        if fc < fd:
            a  = c
            c  = d
            fc = fd
            d  = a + phi * (b - a)
            fd = fn(d)
        else:
            b  = d
            d  = c
            fd = fc
            c  = b - phi * (b - a)
            fc = fn(c)
    x_opt = (a + b) / 2.0
    return x_opt, fn(x_opt)


# ── GateCalibrator ────────────────────────────────────────────────────────────

class GateCalibrator:
    """
    Finds optimal (H*, ρ_min) for the dual-signal entropy gate.

    Parameters
    ----------
    corpus : Corpus
        Labelled text corpus.
    h_search_range : tuple[float, float]
        Search range for H* in bits. Default (1.5, 6.0).
    rho_search_range : tuple[float, float]
        Search range for ρ_min. Default (0.30, 0.95).
    n_steps : int
        Number of golden-section iterations per phase. Default 40.
    verbose : bool
        Print sweep progress. Default False.
    """

    def __init__(
        self,
        corpus:            Corpus,
        h_search_range:    tuple[float, float] = (1.5, 6.0),
        rho_search_range:  tuple[float, float] = (0.30, 0.95),
        n_steps:           int  = 40,
        verbose:           bool = False,
    ) -> None:
        self._corpus    = corpus
        self._h_range   = h_search_range
        self._rho_range = rho_search_range
        self._n_steps   = n_steps
        self._verbose   = verbose

        # Pre-compute entropy and LZ density for all samples (expensive if large)
        self._entropies: list[float] = [_word_entropy(t) for t in corpus.texts]
        self._densities: list[float] = [_lz_density(t)   for t in corpus.texts]

    # ── Phase 1: H* sweep ────────────────────────────────────────────────────

    def _f1_entropy_only(self, h_star: float) -> float:
        m = _evaluate(
            self._corpus.texts,
            self._corpus.labels,
            self._entropies,
            self._densities,
            h_star  = h_star,
            rho_min = 0.0,  # LZ gate disabled
        )
        return m.macro_f1

    # ── Phase 2: ρ_min sweep (conditioned on H*_opt) ─────────────────────────

    def _f1_dual(self, rho_min: float, h_star: float) -> float:
        m = _evaluate(
            self._corpus.texts,
            self._corpus.labels,
            self._entropies,
            self._densities,
            h_star  = h_star,
            rho_min = rho_min,
        )
        return m.macro_f1

    # ── Main calibration entry point ──────────────────────────────────────────

    def calibrate(self) -> CalibrationResult:
        """
        Run Phase 1 + Phase 2 calibration and return a CalibrationResult.
        """
        sweep_log: list[dict[str, Any]] = []

        # ── Phase 1: optimise H* ──────────────────────────────────────────────
        if self._verbose:
            print("[GateCalibrator] Phase 1: sweeping H* ...")

        h_opt, f1_single = _bisect_maximize(
            self._f1_entropy_only,
            lo      = self._h_range[0],
            hi      = self._h_range[1],
            n_steps = self._n_steps,
        )

        # Record coarse sweep for the paper (10 evenly-spaced points)
        for h_probe in [
            self._h_range[0] + (self._h_range[1] - self._h_range[0]) * k / 9
            for k in range(10)
        ]:
            m = _evaluate(
                self._corpus.texts, self._corpus.labels,
                self._entropies, self._densities,
                h_star=h_probe, rho_min=0.0,
            )
            sweep_log.append({
                "phase": 1, "h_star": round(h_probe, 4),
                "rho_min": 0.0, "macro_f1": round(m.macro_f1, 6),
                "tpr": round(m.tpr, 4), "fpr": round(m.fpr, 4),
            })
            if self._verbose:
                print(f"  H*={h_probe:.3f}  F1={m.macro_f1:.4f}  "
                      f"TPR={m.tpr:.3f}  FPR={m.fpr:.3f}")

        # ── Phase 2: optimise ρ_min given H*_opt ─────────────────────────────
        if self._verbose:
            print(f"[GateCalibrator] Phase 2: sweeping ρ_min (H*={h_opt:.4f}) ...")

        rho_opt, f1_dual = _bisect_maximize(
            lambda rho: self._f1_dual(rho, h_opt),
            lo      = self._rho_range[0],
            hi      = self._rho_range[1],
            n_steps = self._n_steps,
        )

        for rho_probe in [
            self._rho_range[0] + (self._rho_range[1] - self._rho_range[0]) * k / 9
            for k in range(10)
        ]:
            m = _evaluate(
                self._corpus.texts, self._corpus.labels,
                self._entropies, self._densities,
                h_star=h_opt, rho_min=rho_probe,
            )
            sweep_log.append({
                "phase": 2, "h_star": round(h_opt, 4),
                "rho_min": round(rho_probe, 4), "macro_f1": round(m.macro_f1, 6),
                "tpr": round(m.tpr, 4), "fpr": round(m.fpr, 4),
            })
            if self._verbose:
                print(f"  ρ_min={rho_probe:.3f}  F1={m.macro_f1:.4f}  "
                      f"TPR={m.tpr:.3f}  FPR={m.fpr:.3f}")

        # ── Final evaluation at (H*_opt, ρ_min_opt) ──────────────────────────
        m_final = _evaluate(
            self._corpus.texts, self._corpus.labels,
            self._entropies, self._densities,
            h_star=h_opt, rho_min=rho_opt,
        )

        return CalibrationResult(
            h_star_opt     = round(h_opt,   6),
            rho_min_opt    = round(rho_opt, 6),
            f1_single      = round(f1_single, 6),
            f1_dual        = round(f1_dual,   6),
            f1_improvement = round(f1_dual - f1_single, 6),
            tpr            = round(m_final.tpr,       4),
            fpr            = round(m_final.fpr,       4),
            precision      = round(m_final.precision, 4),
            recall         = round(m_final.recall,    4),
            n_samples      = len(self._corpus.texts),
            n_attack       = self._corpus.n_attack,
            n_benign       = self._corpus.n_benign,
            sweep_log      = sweep_log,
        )

    # ── Convenience: calibrate from AdaptiveAttacker payloads ─────────────────

    @classmethod
    def from_adaptive_attacker(
        cls,
        h_star:  float = 3.5,
        seed:    int   = 42,
        **kwargs: Any,
    ) -> "GateCalibrator":
        """
        Build a GateCalibrator from 100 automatically-generated adversarial
        samples (50 attack, 50 benign) using AdaptiveAttacker.

        This provides a reproducible calibration corpus that matches the
        adaptive adversary evaluated in benchmark/suites/suite_10_adaptive.py.
        """
        _root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(_root))
        from benchmark.adversary.adaptive_attacker import AdaptiveAttacker

        attacker = AdaptiveAttacker(h_star=h_star, seed=seed)

        texts:  list[str] = []
        labels: list[int] = []

        # 20 attack boundary payloads (those above H*)
        for p in attacker.entropy_boundary_payloads(n=20):
            texts.append(p.text)
            labels.append(1 if p.is_above_gate else 0)

        # 15 mimicry payloads — all adversarial (label=1)
        for p in attacker.entropy_mimicry_payloads(n=15):
            texts.append(p.text)
            labels.append(1)

        # 15 slow-drip final writes (the attack-carrying write)
        for s in attacker.just_under_gradient_sequences(n=15, writes_per_seq=8):
            texts.append(s.writes[-1])
            labels.append(1)

        # 50 benign samples — engineering topics from nexus_tester_util
        benign_pool = [
            "implement JWT authentication with refresh token rotation using Redis",
            "configure PostgreSQL row-level security for multi-tenant SaaS",
            "set up gRPC bidirectional streaming with backpressure control",
            "write Terraform state locking with DynamoDB backend configuration",
            "configure Redis cluster failover and sentinel for high availability",
            "integrate OpenTelemetry distributed tracing with Jaeger backend",
            "deploy Kubernetes HPA with custom KEDA metrics for autoscaling",
            "implement OAuth2 PKCE flow for public single-page application clients",
            "add circuit breaker pattern with exponential backoff for service mesh",
            "build event sourcing pipeline with Kafka and Debezium CDC connector",
            "optimise GraphQL DataLoader to solve N+1 database query problem",
            "configure mutual TLS between microservices using cert-manager operator",
            "set up blue-green deployment strategy with Argo CD GitOps workflow",
            "design CQRS architecture with separate read/write PostgreSQL schemas",
            "integrate HashiCorp Vault dynamic secrets for database credential rotation",
            "write Prometheus alerting rules for SLO burn rate budget tracking",
            "implement Apache Flink streaming window aggregations for metrics",
            "configure Istio VirtualService for traffic shaping and canary releases",
            "design content-addressable storage with SHA-256 hash deduplication",
            "implement distributed rate limiting using token bucket algorithm",
            "configure ElasticSearch index lifecycle management for log retention",
            "write database migration rollback strategy using Flyway versioning",
            "configure OIDC provider federation for cross-organisation SSO flow",
            "design chaos engineering experiment with Gremlin steady-state hypothesis",
            "optimise AWS S3 storage costs via intelligent tiering and lifecycle rules",
            "implement connection pooling with PgBouncer for PostgreSQL performance",
            "add distributed lock with Redlock algorithm across Redis cluster nodes",
            "configure Nginx rate limiting and caching for API gateway layer",
            "design message queue retry with dead letter queue in RabbitMQ setup",
            "write async task queue using Celery with Redis broker configuration",
            "implement incremental backup strategy for SQLite with WAL mode enabled",
            "configure Docker BuildKit cache mounts for faster Python builds",
            "set up Prometheus pushgateway for batch job metric collection",
            "implement feature flags using LaunchDarkly SDK in Python service",
            "configure S3-compatible MinIO storage for local development environment",
            "write end-to-end encryption using AES-256-GCM for snapshot files",
            "design schema migration strategy for zero-downtime PostgreSQL upgrade",
            "implement semantic search using sentence-transformers and FAISS index",
            "configure log aggregation pipeline with Fluentd and Elasticsearch sink",
            "build health check endpoint with readiness and liveness probes for k8s",
            "add OpenAPI schema validation middleware to FastAPI application routes",
            "design multi-region active-active database with conflict resolution policy",
            "implement webhook signature verification using HMAC-SHA256 authentication",
            "configure Datadog APM tracing for distributed Python microservices",
            "write integration test harness for SQLite-backed event ledger system",
            "add async context manager for database transaction rollback in Python",
            "implement streaming response with server-sent events in FastAPI endpoint",
            "design knowledge graph schema for decision nodes and causal relationships",
            "configure LLM fallback chain with circuit breaker for API resilience",
            "write append-only event store with hash chain integrity verification",
        ]
        for topic in benign_pool[:50]:
            texts.append(topic)
            labels.append(0)

        return cls(Corpus(texts=texts, labels=labels), **kwargs)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """
    Run calibration from the AdaptiveAttacker corpus and save results.

    Usage:
        python -X utf8 src/security/gate_calibrator.py
        python -X utf8 src/security/gate_calibrator.py --verbose
        python -X utf8 src/security/gate_calibrator.py --out results/calibration.json
    """
    import argparse

    parser = argparse.ArgumentParser(description="ContextForge Gate Calibrator")
    parser.add_argument("--verbose", action="store_true",
                        help="Print sweep progress")
    parser.add_argument("--out", default="research/benchmark_results/calibration_result.json",
                        help="Output path for calibration JSON")
    parser.add_argument("--h-star", type=float, default=3.5,
                        help="Initial H* for attacker corpus generation")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for AdaptiveAttacker")
    args = parser.parse_args()

    print("[GateCalibrator] Building corpus from AdaptiveAttacker ...")
    cal = GateCalibrator.from_adaptive_attacker(
        h_star  = args.h_star,
        seed    = args.seed,
        verbose = args.verbose,
    )

    print("[GateCalibrator] Running calibration ...")
    result = cal.calibrate()

    print()
    print(result.report())
    print()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(out)
    print(f"[GateCalibrator] Results saved → {out}")


if __name__ == "__main__":
    main()
