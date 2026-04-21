# RATIONALE: Weighted Composite Safety Index (Φ) with configurable weights,
# input validation, 95% confidence interval for N≥30, and three named presets.
"""
Weighted Composite Safety Index
================================

Φ = w₀·ABR + w₁·Δlatency_norm + w₂·TNR

where
  ABR            — Adversarial Block Rate      ∈ [0, 1]  higher-is-better
  Δlatency_norm  — 1 − (latency / latency_max) ∈ [0, 1]  higher-is-better
  TNR            — Token Noise Reduction        ∈ [0, 1]  higher-is-better
  w₀+w₁+w₂ = 1.0 (must be satisfied; raises ValueError otherwise)

Because all three inputs are in [0, 1] and weights sum to 1, Φ ∈ [0, 1].

Named presets
─────────────
  "ide_workflow"        (0.5, 0.3, 0.2) — default; adversarial safety dominant
  "backend_automation"  (0.3, 0.4, 0.3) — latency matters as much as security
  "research_pipeline"   (0.4, 0.2, 0.4) — context quality / TNR dominant

Usage
─────
  from src.metrics.safety_index import WeightedSafetyIndex, PRESETS

  idx = WeightedSafetyIndex()                     # ide_workflow defaults
  result = idx.compute(abr=0.90, latency_ms=130,
                       latency_max_ms=480, tnr=0.70)
  print(result.phi)   # scalar Φ

  # With 95% CI (N≥30 samples required)
  result = idx.compute_multi(
      abr_values=[0.88, 0.90, 0.91, ...],
      latency_values=[128, 131, 135, ...],
      tnr_values=[0.69, 0.70, 0.71, ...],
      latency_max_ms=480,
  )
  print(result.phi_mean, result.phi_ci95)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ── Named presets ──────────────────────────────────────────────────────────────

PRESETS: dict[str, tuple[float, float, float]] = {
    "ide_workflow":       (0.5, 0.3, 0.2),
    "backend_automation": (0.3, 0.4, 0.3),
    "research_pipeline":  (0.4, 0.2, 0.4),
}

_WEIGHT_TOL = 1e-6   # tolerance for sum-to-1 check


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class SafetyResult:
    """Single-point Φ computation result."""
    phi:            float         # Weighted Composite Safety Index
    abr:            float
    latency_norm:   float         # 1 − latency/latency_max
    tnr:            float
    weights:        tuple[float, float, float]
    profile:        str           # preset name or "custom"


@dataclass
class MultiSafetyResult:
    """Multi-sample Φ computation with mean, std, and 95% CI."""
    phi_mean:       float
    phi_std:        float
    phi_ci95:       float         # ±1.96·σ/√N  (populated only if N≥30)
    phi_min:        float
    phi_max:        float
    n_samples:      int
    weights:        tuple[float, float, float]
    profile:        str
    ci_valid:       bool          # True when N≥30


# ── Core class ────────────────────────────────────────────────────────────────

class WeightedSafetyIndex:
    """
    Weighted Composite Safety Index Φ = w₀·ABR + w₁·Δlatency + w₂·TNR.

    Parameters
    ----------
    weights : (w_abr, w_latency, w_tnr)
        Must sum to 1.0 ± 1e-6.  Defaults to the "ide_workflow" preset.
    profile : str
        Human-readable label.  Set automatically when using ``from_preset()``.

    Raises
    ------
    ValueError
        If weights do not sum to 1.0 or any weight is outside [0, 1].
    """

    MIN_N_FOR_CI: int = 30

    def __init__(
        self,
        weights: tuple[float, float, float] = PRESETS["ide_workflow"],
        profile: str = "ide_workflow",
    ) -> None:
        self._validate_weights(weights)
        self._weights: tuple[float, float, float] = tuple(weights)  # type: ignore[assignment]
        self._profile: str = profile

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_preset(cls, name: str) -> "WeightedSafetyIndex":
        """
        Construct from a named preset.

        Parameters
        ----------
        name : "ide_workflow" | "backend_automation" | "research_pipeline"

        Raises
        ------
        KeyError
            If name is not in PRESETS.
        """
        if name not in PRESETS:
            raise KeyError(
                f"Unknown preset {name!r}. Available: {list(PRESETS)}"
            )
        return cls(weights=PRESETS[name], profile=name)

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_weights(weights: tuple[float, float, float]) -> None:
        if len(weights) != 3:
            raise ValueError(
                f"Expected exactly 3 weights, got {len(weights)}."
            )
        for i, w in enumerate(weights):
            if not (0.0 <= w <= 1.0):
                raise ValueError(
                    f"Weight[{i}]={w} is outside [0, 1]."
                )
        total = sum(weights)
        if abs(total - 1.0) > _WEIGHT_TOL:
            raise ValueError(
                f"Weights must sum to 1.0 (got {total:.8f}). "
                f"Adjust your weights so w₀+w₁+w₂=1.0."
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_latency(latency_ms: float, latency_max_ms: float) -> float:
        """
        Normalise latency to [0, 1] where 1 is best (fastest).

        Φ = 1 − latency / latency_max.  Clamped to [0, 1].
        """
        if latency_max_ms <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - latency_ms / latency_max_ms))

    def _phi_scalar(
        self, abr: float, latency_norm: float, tnr: float
    ) -> float:
        w0, w1, w2 = self._weights
        return w0 * abr + w1 * latency_norm + w2 * tnr

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def weights(self) -> tuple[float, float, float]:
        return self._weights

    @property
    def profile(self) -> str:
        return self._profile

    def compute(
        self,
        abr:            float,
        latency_ms:     float,
        latency_max_ms: float,
        tnr:            float,
    ) -> SafetyResult:
        """
        Compute Φ for a single (ABR, latency, TNR) observation.

        Parameters
        ----------
        abr            : Adversarial Block Rate ∈ [0, 1]
        latency_ms     : Mean failover/response latency in milliseconds
        latency_max_ms : Worst-case baseline latency used for normalisation
        tnr            : Token Noise Reduction ∈ [0, 1]

        Returns
        -------
        SafetyResult
        """
        lat_norm = self._norm_latency(latency_ms, latency_max_ms)
        phi      = self._phi_scalar(abr, lat_norm, tnr)
        return SafetyResult(
            phi          = round(phi, 6),
            abr          = abr,
            latency_norm = round(lat_norm, 6),
            tnr          = tnr,
            weights      = self._weights,
            profile      = self._profile,
        )

    def compute_multi(
        self,
        abr_values:     list[float],
        latency_values: list[float],
        tnr_values:     list[float],
        latency_max_ms: float,
    ) -> MultiSafetyResult:
        """
        Compute Φ over N paired observations and return mean ± 95% CI.

        95% CI is reported when N≥30; otherwise ci95 is set to NaN and
        ci_valid=False.

        Parameters
        ----------
        abr_values     : list of per-run ABR values
        latency_values : list of per-run mean latency in ms
        tnr_values     : list of per-run TNR values
        latency_max_ms : normalisation constant (e.g. worst baseline latency)

        Raises
        ------
        ValueError
            If the three lists have different lengths or are empty.
        """
        n = len(abr_values)
        if n == 0:
            raise ValueError("Empty input lists — cannot compute Φ.")
        if len(latency_values) != n or len(tnr_values) != n:
            raise ValueError(
                f"All input lists must have the same length "
                f"(got {n}, {len(latency_values)}, {len(tnr_values)})."
            )

        phi_vals: list[float] = []
        for a, l, t in zip(abr_values, latency_values, tnr_values):
            ln = self._norm_latency(l, latency_max_ms)
            phi_vals.append(self._phi_scalar(a, ln, t))

        mean = sum(phi_vals) / n
        var  = sum((v - mean) ** 2 for v in phi_vals) / (n - 1) if n > 1 else 0.0
        std  = math.sqrt(var)
        ci_valid = n >= self.MIN_N_FOR_CI
        ci95     = (1.96 * std / math.sqrt(n)) if ci_valid else float("nan")

        return MultiSafetyResult(
            phi_mean  = round(mean, 6),
            phi_std   = round(std,  6),
            phi_ci95  = round(ci95, 6) if ci_valid else float("nan"),
            phi_min   = round(min(phi_vals), 6),
            phi_max   = round(max(phi_vals), 6),
            n_samples = n,
            weights   = self._weights,
            profile   = self._profile,
            ci_valid  = ci_valid,
        )

    def __repr__(self) -> str:
        w = self._weights
        return (
            f"WeightedSafetyIndex(profile={self._profile!r}, "
            f"w_abr={w[0]}, w_lat={w[1]}, w_tnr={w[2]})"
        )
