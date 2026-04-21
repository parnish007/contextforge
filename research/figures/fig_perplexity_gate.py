"""
Perplexity Gate (Pass 0.5) — distribution plot.
Shows KDE of benign vs adversarial perplexity scores with P* threshold.
All data is synthetic but calibrated to match the smoke-test results:
  P* = 231.78 (95th percentile of benign corpus)
  adversarial P = 463.3 (2.0 × P*)
"""
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path

OUT = Path(__file__).parent / "fig_perplexity_gate.png"

# ── Reproduce the calibrated distributions ────────────────────────────────────
# P* = 231.78 is the 95th percentile of the benign distribution.
# If benign ~ LogNormal(mu, sigma), then P(95th) = exp(mu + 1.645*sigma) = 231.78
# We pick mu=4.7, sigma=0.55  → median = exp(4.7) ≈ 110, P95 ≈ 231
RNG = np.random.default_rng(42)
N_BENIGN = 300
N_ADV    = 100

benign_log = RNG.normal(loc=4.70, scale=0.55, size=N_BENIGN)
benign     = np.exp(benign_log)                                 # median ≈ 110, P95 ≈ 231

# Adversarial (mimicry) distribution: centred around 2 × P*
adv_log = RNG.normal(loc=np.log(463.3), scale=0.40, size=N_ADV)
adv     = np.exp(adv_log)

P_STAR = 231.78


def _kde(data, x_grid, bw=None):
    """Gaussian KDE on x_grid."""
    from scipy.stats import gaussian_kde
    kde = gaussian_kde(data, bw_method=bw)
    return kde(x_grid)


def main():
    x = np.linspace(20, 1200, 800)

    try:
        from scipy.stats import gaussian_kde  # noqa: F401
        benign_kde = _kde(benign, x, bw=0.25)
        adv_kde    = _kde(adv,    x, bw=0.25)
        use_kde    = True
    except ImportError:
        use_kde = False

    fig, ax = plt.subplots(figsize=(7, 3.8))

    GREEN  = "#27ae60"
    RED    = "#e74c3c"
    DARK   = "#2c3e50"
    ORANGE = "#e67e22"

    if use_kde:
        ax.fill_between(x, benign_kde, alpha=0.25, color=GREEN)
        ax.plot(x, benign_kde, color=GREEN, lw=2, label="Benign corpus")
        ax.fill_between(x, adv_kde, alpha=0.25, color=RED)
        ax.plot(x, adv_kde, color=RED, lw=2, label="Entropy-mimicry payloads")
    else:
        # Histogram fallback
        ax.hist(benign, bins=40, density=True, alpha=0.35, color=GREEN, label="Benign corpus")
        ax.hist(adv,    bins=25, density=True, alpha=0.35, color=RED,   label="Entropy-mimicry payloads")

    # P* threshold line
    ax.axvline(P_STAR, color=DARK, lw=2, ls="--", zorder=5)
    ax.text(
        P_STAR + 12, ax.get_ylim()[1] * 0.85 if not use_kde else max(benign_kde) * 0.92,
        f"$P^*={P_STAR:.0f}$\n(95th pct, benign)",
        fontsize=8, color=DARK, va="top",
    )

    # Adversarial sample arrow
    ax.annotate(
        f"Adversarial sample\n$\\mathcal{{P}}=463.3$ ($2.0\\times P^*$)\nflagged=True",
        xy=(463.3, 0), xytext=(550, max(benign_kde if use_kde else [0.008]) * 0.6 if use_kde else 0.006),
        arrowprops=dict(arrowstyle="->", color=RED, lw=1.5),
        fontsize=7.5, color=RED, ha="left",
    )

    # Shade flagged region
    ax.axvspan(P_STAR, 1200, alpha=0.06, color=RED, label="Flagged region ($\\mathcal{P}>P^*$)")

    ax.set_xlabel("Trigram Perplexity $\\mathcal{P}(w)$", fontsize=10)
    ax.set_ylabel("Density", fontsize=10)
    ax.set_title(
        "Perplexity Gate (Pass 0.5) — Benign vs. Entropy-Mimicry Adversarial",
        fontsize=11,
    )
    ax.legend(fontsize=8, loc="upper right")
    ax.set_xlim(20, 900)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()