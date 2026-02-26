from __future__ import annotations

import math

import numpy as np

from starrage_sim.config import GlobalNoise, TissueParams


def mark_decay(t: np.ndarray, half_life: float) -> np.ndarray:
    return np.exp(-np.log(2.0) * t / max(half_life, 1e-9))


def stress_term(amplification: float, cyclin_throttle: float, noise: GlobalNoise) -> float:
    return noise.stress_scale * (amplification**2 + cyclin_throttle**2 + amplification * cyclin_throttle)


def mutation_rate(
    mu0: float,
    amplification: float,
    cyclin_throttle: float,
    mark_on: np.ndarray,
    tissue_params: TissueParams,
    noise: GlobalNoise,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    eps = rng.lognormal(mean=0.0, sigma=noise.mutation_noise_sigma, size=mark_on.shape[0])
    denominator = (1.0 + tissue_params.alpha * amplification * mark_on) * (
        1.0 + tissue_params.beta * cyclin_throttle
    )
    stress = stress_term(amplification, cyclin_throttle, noise)
    treated = (mu0 * eps / np.maximum(denominator, 1e-8)) + noise.stress_penalty * stress
    baseline = mu0 * eps
    return baseline, treated


def viability(
    amplification: float,
    cyclin_throttle: float,
    tissue_params: TissueParams,
    noise: GlobalNoise,
    rng: np.random.Generator,
) -> float:
    stress = stress_term(amplification, cyclin_throttle, noise)
    value = (
        tissue_params.v0
        - tissue_params.p1 * amplification**2
        - tissue_params.p2 * cyclin_throttle**2
        - tissue_params.p3 * amplification * cyclin_throttle
        - tissue_params.p4 * stress
        + float(rng.normal(0.0, noise.viability_noise_sigma))
    )
    return float(np.clip(value, 0.0, 1.0))


def fucci_expression(
    amplification: float,
    mark_on: float,
    leakage_baseline: float,
    expression_noise_sigma: float,
    leakage_noise_sigma: float,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    baseline = 1.0
    expr_s = baseline + amplification * mark_on + float(rng.normal(0.0, expression_noise_sigma))
    leak = float(np.clip(leakage_baseline + rng.normal(0.0, leakage_noise_sigma), 0.02, 0.95))
    expr_g1 = baseline * leak + float(rng.normal(0.0, expression_noise_sigma))
    expr_s = max(expr_s, 0.05)
    expr_g1 = max(expr_g1, 0.05)
    return expr_s, expr_g1, leak


def logistic_synergy(amplification: float, cyclin_throttle: float, k: float) -> float:
    x = amplification * cyclin_throttle
    return 1.0 / (1.0 + math.exp(-k * (x - 0.15)))
