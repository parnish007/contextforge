"""
Entropy gate figure: two Gaussian distributions (benign, adversarial)
with H* = 3.5 gate line and VOH line at 4.375.
Data from benchmark/engine.py measurements: benign mu=2.74, sigma=0.43; adv mu=3.94, sigma=0.79.
"""
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

OUT = Path(__file__).parent / "fig_entropy_gate.png"


def main():
    mu_b, sig_b = 2.74, 0.43   # benign
    mu_a, sig_a = 3.94, 0.79   # adversarial

    x = np.linspace(0.5, 7.0, 600)
    pdf_b = (1 / (sig_b * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mu_b) / sig_b) ** 2)
    pdf_a = (1 / (sig_a * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mu_a) / sig_a) ** 2)

    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.fill_between(x, pdf_b, alpha=0.30, color="#2ecc71", label=None)
    ax.plot(x, pdf_b, color="#27ae60", lw=2, label=r"Benign ($\mu=2.74$, $\sigma=0.43$)")
    ax.fill_between(x, pdf_a, alpha=0.25, color="#e74c3c", label=None)
    ax.plot(x, pdf_a, color="#c0392b", lw=2, label=r"Adversarial ($\mu=3.94$, $\sigma=0.79$)")

    # Gate lines
    ax.axvline(3.5,   color="#c0392b", lw=1.8, ls="--", label=r"$H^*=3.5$ bits (standard gate)")
    ax.axvline(4.375, color="#8e44ad", lw=1.4, ls=":",  label=r"$H^*_\mathrm{VOH}=4.375$ bits")

    # Annotation
    ax.annotate(
        "0 benign probes\nexceed the gate",
        xy=(3.5, 0.72), xytext=(4.0, 0.82),
        arrowprops=dict(arrowstyle="->", color="#27ae60"),
        color="#27ae60", fontsize=8,
    )

    ax.set_xlabel("Shannon Entropy $H$ (bits)", fontsize=10)
    ax.set_ylabel("Probability Density", fontsize=10)
    ax.set_title("Entropy Distributions of Benign vs. Adversarial Payloads", fontsize=11)
    ax.legend(fontsize=8, loc="upper right")
    ax.set_xlim(0.5, 7.0)
    ax.set_ylim(0)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=300, bbox_inches="tight")
    print(f"  Saved: {OUT}")
    plt.close(fig)


if __name__ == "__main__":
    main()
