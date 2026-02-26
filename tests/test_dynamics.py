from __future__ import annotations

import numpy as np

from starrage_sim.config import GlobalNoise, TissueParams
from starrage_sim.model.dynamics import mark_decay, mutation_rate, viability


def _default_noise() -> GlobalNoise:
    return GlobalNoise(
        mutation_noise_sigma=0.1,
        viability_noise_sigma=0.01,
        expression_noise_sigma=0.1,
        stress_scale=0.5,
        stress_penalty=0.02,
        leakage_baseline=0.35,
        leakage_noise_sigma=0.03,
    )


def _default_params() -> TissueParams:
    return TissueParams(alpha=1.0, beta=0.2, v0=0.97, p1=0.01, p2=0.08, p3=0.03, p4=0.03)


def test_stronger_amplification_reduces_mutation_rate_without_extreme_stress() -> None:
    rng = np.random.default_rng(11)
    noise = _default_noise()
    params = _default_params()
    t = np.arange(1, 40)
    mark_on = mark_decay(t, half_life=100.0)

    _, treated_low = mutation_rate(1.2, 0.8, 0.08, mark_on, params, noise, rng)
    _, treated_high = mutation_rate(1.2, 1.6, 0.08, mark_on, params, noise, rng)

    assert treated_high.mean() < treated_low.mean()


def test_extreme_cyclin_throttle_reduces_viability() -> None:
    rng = np.random.default_rng(3)
    noise = _default_noise()
    params = _default_params()

    v_low = viability(1.0, 0.05, params, noise, rng)
    v_high = viability(1.0, 0.25, params, noise, rng)

    assert v_high < v_low
