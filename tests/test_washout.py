from __future__ import annotations

import numpy as np

from starrage_sim.metrics.washout import fit_half_life, fit_washout_curve


def test_fit_half_life_recovers_known_signal() -> None:
    rng = np.random.default_rng(7)
    true_half_life = 60.0
    t = np.linspace(1, 80, 12)
    y = np.exp(-np.log(2) * t / true_half_life)
    y = y * (1.0 + rng.normal(0.0, 0.02, size=t.shape[0]))
    y = np.clip(y, 1e-9, None)

    est, r2 = fit_half_life(t, y)

    assert 45.0 <= est <= 75.0
    assert r2 > 0.9


def test_fit_washout_curve_supports_auto_bi_model() -> None:
    rng = np.random.default_rng(13)
    t = np.linspace(1, 100, 16)
    y = 0.65 * np.exp(-np.log(2) * t / 30.0) + 0.35 * np.exp(-np.log(2) * t / 140.0)
    y = y * (1.0 + rng.normal(0.0, 0.03, size=t.shape[0]))
    y = np.clip(y, 1e-9, None)

    fit = fit_washout_curve(
        times=t,
        marks=y,
        model_family="auto_single_or_bi",
        holdout_fraction=0.25,
        retention_horizon_divisions=80.0,
    )

    assert np.isfinite(float(fit["half_life"]))
    assert 0.0 <= float(fit["predictive_coverage"]) <= 1.0
    assert float(fit["predictive_median_abs_log_error"]) >= 0.0
    assert float(fit["retained_effect_at_horizon"]) > 0.0
