from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import numpy as np


_LN2 = math.log(2.0)
_EPS = 1e-12


@dataclass(frozen=True)
class _FitCandidate:
    model_name: str
    predictor: Callable[[np.ndarray], np.ndarray]
    train_score: float
    half_life: float


def _safe_log(values: np.ndarray) -> np.ndarray:
    return np.log(np.clip(values, _EPS, None))


def _r2_logspace(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    logy = _safe_log(y_true)
    logp = _safe_log(y_pred)
    y_mean = float(np.mean(logy))
    ss_res = float(np.sum((logy - logp) ** 2))
    ss_tot = float(np.sum((logy - y_mean) ** 2))
    return 1.0 if ss_tot <= _EPS else max(0.0, 1.0 - ss_res / ss_tot)


def _fit_linear_logspace(x: np.ndarray, logy: np.ndarray) -> tuple[float, float]:
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(logy))
    cov = float(np.sum((x - x_mean) * (logy - y_mean)))
    var = float(np.sum((x - x_mean) ** 2))
    if var <= 1e-12:
        return 0.0, 0.0
    slope = cov / var
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _theil_sen_slope(x: np.ndarray, y: np.ndarray) -> float:
    n = x.size
    if n < 3:
        return 0.0
    slopes: list[float] = []
    for i in range(n - 1):
        dx = x[i + 1 :] - x[i]
        dy = y[i + 1 :] - y[i]
        valid = np.abs(dx) > 1e-12
        if np.any(valid):
            slopes.extend((dy[valid] / dx[valid]).tolist())
    if not slopes:
        return 0.0
    return float(np.median(np.asarray(slopes, dtype=float)))


def _single_predictor(amplitude: float, slope: float) -> Callable[[np.ndarray], np.ndarray]:
    def _predict(x: np.ndarray) -> np.ndarray:
        return np.clip(amplitude * np.exp(slope * x), _EPS, None)

    return _predict


def _biexp_predictor(amplitude: float, weight_fast: float, h_fast: float, h_slow: float) -> Callable[[np.ndarray], np.ndarray]:
    decay_fast = _LN2 / max(h_fast, _EPS)
    decay_slow = _LN2 / max(h_slow, _EPS)

    def _predict(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        base = weight_fast * np.exp(-decay_fast * x) + (1.0 - weight_fast) * np.exp(-decay_slow * x)
        return np.clip(amplitude * base, _EPS, None)

    return _predict


def _half_life_from_predictor(predictor: Callable[[np.ndarray], np.ndarray]) -> float:
    y0 = float(predictor(np.asarray([0.0], dtype=float))[0])
    if not np.isfinite(y0) or y0 <= 0.0:
        return float("inf")
    target = 0.5 * y0

    lo = 0.0
    hi = 1.0
    for _ in range(40):
        y_hi = float(predictor(np.asarray([hi], dtype=float))[0])
        if not np.isfinite(y_hi):
            return float("inf")
        if y_hi <= target:
            break
        hi *= 2.0
        if hi > 1_000_000.0:
            return float("inf")

    for _ in range(60):
        mid = 0.5 * (lo + hi)
        y_mid = float(predictor(np.asarray([mid], dtype=float))[0])
        if y_mid > target:
            lo = mid
        else:
            hi = mid
    return float(0.5 * (lo + hi))


def _fit_single_exponential(x: np.ndarray, y: np.ndarray) -> _FitCandidate:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    mask = np.isfinite(x) & np.isfinite(y) & (y > 0)
    if mask.sum() < 3:
        return _FitCandidate(
            model_name="single_exponential_robust_logspace",
            predictor=lambda v: np.full_like(np.asarray(v, dtype=float), np.nan, dtype=float),
            train_score=float("inf"),
            half_life=float("inf"),
        )

    x = x[mask]
    y = y[mask]
    logy = np.log(y)

    slope = _theil_sen_slope(x, logy)
    intercept = float(np.median(logy - slope * x))
    pred = intercept + slope * x
    resid = logy - pred

    mad = float(np.median(np.abs(resid)))
    scale = max(mad * 1.4826, 1e-8)
    inlier = np.abs(resid) <= 2.5 * scale
    if int(np.sum(inlier)) >= 3:
        slope_refined, intercept_refined = _fit_linear_logspace(x[inlier], logy[inlier])
        if slope_refined != 0.0 or intercept_refined != 0.0:
            slope = slope_refined
            intercept = intercept_refined
            pred = intercept + slope * x

    amplitude = math.exp(intercept)
    predictor = _single_predictor(amplitude=amplitude, slope=slope)
    pred_marks = predictor(x)
    train_score = float(np.median(np.abs(_safe_log(y) - _safe_log(pred_marks))))
    half_life = float("inf")
    if slope < -1e-12:
        half_life = -_LN2 / slope
    if not np.isfinite(half_life) or half_life <= 0:
        half_life = float("inf")

    return _FitCandidate(
        model_name="single_exponential_robust_logspace",
        predictor=predictor,
        train_score=train_score,
        half_life=half_life,
    )


def _fit_bi_exponential(x: np.ndarray, y: np.ndarray) -> _FitCandidate:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y) & (y > 0)
    if int(np.sum(mask)) < 4:
        return _fit_single_exponential(x, y)

    x = x[mask]
    y = y[mask]
    span = max(float(np.max(x) - np.min(x)), 1.0)
    hl_min = max(2.0, 0.04 * span)
    hl_max = max(20.0, 8.0 * span)
    # Keep this grid intentionally compact so "auto_single_or_bi" remains practical
    # in large Monte Carlo sweeps.
    hl_grid = np.logspace(np.log10(hl_min), np.log10(hl_max), 10)
    weight_grid = np.linspace(0.15, 0.85, 5)

    best_score = float("inf")
    best_predictor: Callable[[np.ndarray], np.ndarray] | None = None

    for i in range(len(hl_grid) - 1):
        h_fast = float(hl_grid[i])
        e_fast = np.exp(-_LN2 * x / h_fast)
        for j in range(i + 1, len(hl_grid)):
            h_slow = float(hl_grid[j])
            e_slow = np.exp(-_LN2 * x / h_slow)
            for weight in weight_grid:
                basis = weight * e_fast + (1.0 - weight) * e_slow
                denom = float(np.dot(basis, basis))
                if denom <= _EPS:
                    continue
                amplitude = float(np.dot(y, basis) / denom)
                if not np.isfinite(amplitude) or amplitude <= 0:
                    continue
                pred = np.clip(amplitude * basis, _EPS, None)
                score = float(np.median(np.abs(_safe_log(y) - _safe_log(pred))))
                if score < best_score:
                    best_score = score
                    best_predictor = _biexp_predictor(
                        amplitude=amplitude,
                        weight_fast=float(weight),
                        h_fast=h_fast,
                        h_slow=h_slow,
                    )

    if best_predictor is None:
        return _fit_single_exponential(x, y)

    return _FitCandidate(
        model_name="bi_exponential",
        predictor=best_predictor,
        train_score=best_score,
        half_life=_half_life_from_predictor(best_predictor),
    )


def _split_train_holdout(
    x: np.ndarray,
    y: np.ndarray,
    holdout_fraction: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = int(x.size)
    if n < 5 or holdout_fraction <= 0.0:
        return x, y, np.asarray([], dtype=float), np.asarray([], dtype=float)

    holdout_n = int(round(n * holdout_fraction))
    holdout_n = min(max(1, holdout_n), max(1, n - 3))
    if holdout_n <= 0:
        return x, y, np.asarray([], dtype=float), np.asarray([], dtype=float)

    hold_idx = np.unique(np.linspace(1, n - 1, num=holdout_n, dtype=int))
    train_mask = np.ones(n, dtype=bool)
    train_mask[hold_idx] = False
    if int(np.sum(train_mask)) < 3:
        return x, y, np.asarray([], dtype=float), np.asarray([], dtype=float)

    return x[train_mask], y[train_mask], x[hold_idx], y[hold_idx]


def fit_washout_curve(
    times: np.ndarray,
    marks: np.ndarray,
    model_family: str,
    holdout_fraction: float = 0.25,
    retention_horizon_divisions: float = 80.0,
) -> dict[str, float | str]:
    x = np.asarray(times, dtype=float)
    y = np.asarray(marks, dtype=float)

    mask = np.isfinite(x) & np.isfinite(y) & (y > 0)
    if int(np.sum(mask)) < 3:
        return {
            "half_life": float("inf"),
            "r2": 0.0,
            "selected_model": str(model_family),
            "predictive_median_abs_log_error": float("inf"),
            "predictive_coverage": 0.0,
            "retained_effect_at_horizon": float("nan"),
            "holdout_n": 0.0,
        }

    x = x[mask]
    y = y[mask]
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    x_train, y_train, x_hold, y_hold = _split_train_holdout(x, y, holdout_fraction=holdout_fraction)

    family = str(model_family).strip().lower()
    if family in {"single_exponential_robust_logspace", "single"}:
        candidates = [_fit_single_exponential(x_train, y_train)]
    elif family in {"bi_exponential", "bi"}:
        candidates = [_fit_bi_exponential(x_train, y_train)]
    elif family in {"auto_single_or_bi", "hierarchical_tissue_mark", "hierarchical"}:
        candidates = [_fit_single_exponential(x_train, y_train), _fit_bi_exponential(x_train, y_train)]
    else:
        candidates = [_fit_single_exponential(x_train, y_train)]

    selected: _FitCandidate | None = None
    selected_score = float("inf")
    for cand in candidates:
        if x_hold.size > 0:
            pred_hold = np.clip(cand.predictor(x_hold), _EPS, None)
            score = float(np.median(np.abs(_safe_log(y_hold) - _safe_log(pred_hold))))
            # Light complexity penalty so bi-exponential is selected only when it materially helps.
            if cand.model_name == "bi_exponential":
                score += 0.005
        else:
            score = float(cand.train_score + (0.005 if cand.model_name == "bi_exponential" else 0.0))
        if score < selected_score:
            selected = cand
            selected_score = score

    if selected is None:
        selected = _fit_single_exponential(x_train, y_train)

    pred_all = np.clip(selected.predictor(x), _EPS, None)
    pred_train = np.clip(selected.predictor(x_train), _EPS, None)
    r2 = _r2_logspace(y, pred_all)

    if x_hold.size > 0:
        pred_hold = np.clip(selected.predictor(x_hold), _EPS, None)
        hold_res = np.abs(_safe_log(y_hold) - _safe_log(pred_hold))
    else:
        pred_hold = pred_train
        hold_res = np.abs(_safe_log(y_train) - _safe_log(pred_train))

    train_res = np.abs(_safe_log(y_train) - _safe_log(pred_train))
    band = float(np.quantile(train_res, 0.90)) if train_res.size else 0.0
    predictive_coverage = float(np.mean(hold_res <= max(band, 1e-6))) if hold_res.size else 0.0
    predictive_mae = float(np.median(hold_res)) if hold_res.size else float("inf")
    retained = float(selected.predictor(np.asarray([float(retention_horizon_divisions)], dtype=float))[0])

    return {
        "half_life": float(selected.half_life),
        "r2": float(r2),
        "selected_model": selected.model_name,
        "predictive_median_abs_log_error": predictive_mae,
        "predictive_coverage": predictive_coverage,
        "retained_effect_at_horizon": retained,
        "holdout_n": float(x_hold.size),
    }


def fit_half_life(times: np.ndarray, marks: np.ndarray) -> tuple[float, float]:
    fit = fit_washout_curve(
        times=times,
        marks=marks,
        model_family="single_exponential_robust_logspace",
        holdout_fraction=0.0,
        retention_horizon_divisions=80.0,
    )
    return float(fit["half_life"]), float(fit["r2"])
