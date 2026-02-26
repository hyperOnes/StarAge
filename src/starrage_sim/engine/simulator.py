from __future__ import annotations

from dataclasses import dataclass
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np

from starrage_sim.config import GlobalNoise, SimulationConfig, TissueParams, WashoutPrior
from starrage_sim.metrics.statistics import (
    deterministic_seed,
    finite_fraction,
    proportion_ci_wilson,
    quantile_ci,
    sem,
    summarize,
)
from starrage_sim.metrics.washout import fit_washout_curve
from starrage_sim.model.dynamics import fucci_expression, logistic_synergy, mark_decay, mutation_rate, viability
from starrage_sim.model.registry import get_baseline_mutation_rate, get_tissue_params, get_washout_prior
from starrage_sim.model.types import DosePair, SweepProfile


@dataclass(frozen=True)
class TaskSpec:
    tissue: str
    donor_age: int
    amplification: float
    cyclin_throttle: float
    replicates: int
    n_divisions: int
    fucci_cells: int
    seed: int
    mu0: float
    tissue_params: TissueParams
    washout_prior: WashoutPrior
    washout_n_timepoints: int
    washout_cv_scale: float
    washout_model_family: str
    washout_holdout_fraction: float
    washout_retention_horizon_divisions: float
    global_noise: GlobalNoise


@dataclass
class SimulationRunResult:
    profile: str
    assumptions_hash: str
    raw_records: list[dict[str, Any]]
    condition_summary: list[dict[str, Any]]
    dose_summary: list[dict[str, Any]]
    tissue_dose_summary: list[dict[str, Any]]
    washout_summary: list[dict[str, Any]]
    best_dose_per_tissue: dict[str, dict[str, Any]]
    pareto: dict[str, Any]
    synergy: dict[str, Any]

    def to_metrics_json(self, thresholds: dict[str, float], config_source: str) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "config_source": config_source,
            "assumptions_hash": self.assumptions_hash,
            "thresholds": thresholds,
            "condition_summary": self.condition_summary,
            "dose_summary": self.dose_summary,
            "tissue_dose_summary": self.tissue_dose_summary,
            "washout_summary": self.washout_summary,
            "best_dose_per_tissue": self.best_dose_per_tissue,
            "pareto": self.pareto,
            "synergy": self.synergy,
        }


def generate_coarse_doses(config: SimulationConfig) -> list[DosePair]:
    amp = np.linspace(
        config.dose_ranges.amplification.min,
        config.dose_ranges.amplification.max,
        config.dose_ranges.amplification.n_coarse,
    )
    cyc = np.linspace(
        config.dose_ranges.cyclin_throttle.min,
        config.dose_ranges.cyclin_throttle.max,
        config.dose_ranges.cyclin_throttle.n_coarse,
    )
    return [DosePair(float(a), float(c)) for a in amp for c in cyc]


def refine_doses(config: SimulationConfig, top_doses: list[DosePair]) -> list[DosePair]:
    amp_axis = config.dose_ranges.amplification
    cyc_axis = config.dose_ranges.cyclin_throttle
    refine = config.dose_ranges.refine

    amp_step = 0.0
    cyc_step = 0.0
    if amp_axis.n_coarse > 1:
        amp_step = ((amp_axis.max - amp_axis.min) / (amp_axis.n_coarse - 1)) * refine.step_fraction
    if cyc_axis.n_coarse > 1:
        cyc_step = ((cyc_axis.max - cyc_axis.min) / (cyc_axis.n_coarse - 1)) * refine.step_fraction

    out: dict[tuple[float, float], DosePair] = {}
    for dose in top_doses:
        for da in range(-refine.neighbor_radius, refine.neighbor_radius + 1):
            for dc in range(-refine.neighbor_radius, refine.neighbor_radius + 1):
                new_a = float(np.clip(dose.amplification + da * amp_step, amp_axis.min, amp_axis.max))
                new_c = float(np.clip(dose.cyclin_throttle + dc * cyc_step, cyc_axis.min, cyc_axis.max))
                candidate = DosePair(new_a, new_c)
                out[candidate.key()] = candidate
    return list(out.values())


def _resolve_workers(profile: SweepProfile) -> int:
    if profile.workers == "auto":
        return max(1, (os.cpu_count() or 2) - 1)
    workers = int(profile.workers)
    return max(1, workers)


def _simulate_task(spec: TaskSpec) -> list[dict[str, Any]]:
    rng = np.random.default_rng(spec.seed)
    t = np.arange(1, spec.n_divisions + 1, dtype=float)
    measure_t = np.linspace(
        1,
        spec.n_divisions,
        num=min(spec.washout_n_timepoints, spec.n_divisions),
        dtype=float,
    )
    records: list[dict[str, Any]] = []

    for rep in range(spec.replicates):
        true_on_hl = spec.washout_prior.on_half_life * float(rng.lognormal(mean=0.0, sigma=0.10))
        true_off_hl = spec.washout_prior.off_half_life * float(rng.lognormal(mean=0.0, sigma=0.10))

        mark_on = mark_decay(t, true_on_hl)
        _, treated_mu = mutation_rate(
            mu0=spec.mu0,
            amplification=spec.amplification,
            cyclin_throttle=spec.cyclin_throttle,
            mark_on=mark_on,
            tissue_params=spec.tissue_params,
            noise=spec.global_noise,
            rng=rng,
        )

        baseline_noise = rng.lognormal(mean=0.0, sigma=spec.global_noise.mutation_noise_sigma, size=t.shape[0])
        baseline_mu = spec.mu0 * baseline_noise
        treated_mu_matched = (
            spec.mu0
            * baseline_noise
            / (
                (1.0 + spec.tissue_params.alpha * spec.amplification * mark_on)
                * (1.0 + spec.tissue_params.beta * spec.cyclin_throttle)
            )
        ) + spec.global_noise.stress_penalty * (
            spec.global_noise.stress_scale
            * (spec.amplification**2 + spec.cyclin_throttle**2 + spec.amplification * spec.cyclin_throttle)
        )

        baseline_mean = max(float(np.mean(baseline_mu)), 1e-9)
        treated_mean = max(float(np.mean(0.5 * (treated_mu + treated_mu_matched))), 1e-9)
        mutation_reduction = baseline_mean / treated_mean

        viability_value = viability(
            amplification=spec.amplification,
            cyclin_throttle=spec.cyclin_throttle,
            tissue_params=spec.tissue_params,
            noise=spec.global_noise,
            rng=rng,
        )

        idx = rng.integers(0, spec.n_divisions, size=spec.fucci_cells)
        folds: list[float] = []
        leaks: list[float] = []
        for i in idx:
            expr_s, expr_g1, leak = fucci_expression(
                amplification=spec.amplification,
                mark_on=float(mark_on[int(i)]),
                leakage_baseline=spec.global_noise.leakage_baseline,
                expression_noise_sigma=spec.global_noise.expression_noise_sigma,
                leakage_noise_sigma=spec.global_noise.leakage_noise_sigma,
                rng=rng,
            )
            folds.append(float(np.clip(expr_s / expr_g1, 0.1, 50.0)))
            leaks.append(leak)

        sphase_fold = float(np.median(folds))
        leakage = float(np.mean(leaks))

        on_obs = mark_decay(measure_t, true_on_hl) * (
            1.0
            + rng.normal(
                0.0,
                spec.washout_prior.measurement_cv * spec.washout_cv_scale,
                size=measure_t.shape[0],
            )
        )
        off_obs = mark_decay(measure_t, true_off_hl) * (
            1.0
            + rng.normal(
                0.0,
                spec.washout_prior.measurement_cv * spec.washout_cv_scale,
                size=measure_t.shape[0],
            )
        )

        on_obs = np.clip(on_obs, 1e-9, None)
        off_obs = np.clip(off_obs, 1e-9, None)

        on_fit = fit_washout_curve(
            times=measure_t,
            marks=on_obs,
            model_family=spec.washout_model_family,
            holdout_fraction=spec.washout_holdout_fraction,
            retention_horizon_divisions=spec.washout_retention_horizon_divisions,
        )
        off_fit = fit_washout_curve(
            times=measure_t,
            marks=off_obs,
            model_family=spec.washout_model_family,
            holdout_fraction=spec.washout_holdout_fraction,
            retention_horizon_divisions=spec.washout_retention_horizon_divisions,
        )

        records.append(
            {
                "tissue": spec.tissue,
                "donor_age": spec.donor_age,
                "amplification": spec.amplification,
                "cyclin_throttle": spec.cyclin_throttle,
                "replicate": rep,
                "mutation_reduction": float(mutation_reduction),
                "viability": float(viability_value),
                "sphase_fold": float(sphase_fold),
                "leakage": float(leakage),
                "on_half_life": float(on_fit["half_life"]),
                "off_half_life": float(off_fit["half_life"]),
                "on_r2": float(on_fit["r2"]),
                "off_r2": float(off_fit["r2"]),
                "on_selected_model": str(on_fit["selected_model"]),
                "off_selected_model": str(off_fit["selected_model"]),
                "on_predictive_median_abs_log_error": float(on_fit["predictive_median_abs_log_error"]),
                "off_predictive_median_abs_log_error": float(off_fit["predictive_median_abs_log_error"]),
                "on_predictive_coverage": float(on_fit["predictive_coverage"]),
                "off_predictive_coverage": float(off_fit["predictive_coverage"]),
                "on_retained_effect_at_horizon": float(on_fit["retained_effect_at_horizon"]),
                "off_retained_effect_at_horizon": float(off_fit["retained_effect_at_horizon"]),
                "washout_model_family": spec.washout_model_family,
            }
        )

    return records


def _summarize_records(records: list[dict[str, Any]], group_cols: list[str]) -> list[dict[str, Any]]:
    metric_cols = ["mutation_reduction", "viability", "sphase_fold", "leakage"]
    grouped: dict[tuple[Any, ...], dict[str, list[float]]] = {}

    for rec in records:
        key = tuple(rec[col] for col in group_cols)
        bucket = grouped.setdefault(key, {metric: [] for metric in metric_cols})
        for metric in metric_cols:
            bucket[metric].append(float(rec[metric]))

    out: list[dict[str, Any]] = []
    for key, values in grouped.items():
        row = {col: key[idx] for idx, col in enumerate(group_cols)}
        for metric in metric_cols:
            stats = summarize(values[metric])
            row[f"{metric}_mean"] = stats["mean"]
            row[f"{metric}_median"] = stats["median"]
            row[f"{metric}_std"] = stats["std"]
            row[f"{metric}_n"] = stats["n"]
            row[f"{metric}_ci_low"] = stats["ci_low"]
            row[f"{metric}_ci_high"] = stats["ci_high"]
        out.append(row)

    out.sort(key=lambda r: tuple(r[c] for c in group_cols))
    return out


def _group_records_by_keys(
    records: list[dict[str, Any]],
    group_cols: list[str],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for rec in records:
        key = tuple(rec[col] for col in group_cols)
        grouped.setdefault(key, []).append(rec)
    return grouped


def _hierarchical_bootstrap_ci(
    recs: list[dict[str, Any]],
    metric: str,
    bootstrap_n: int,
    seed: int,
) -> tuple[float, float, float, int]:
    by_stratum: dict[tuple[str, int], list[float]] = {}
    for rec in recs:
        stratum = (str(rec["tissue"]), int(rec["donor_age"]))
        by_stratum.setdefault(stratum, []).append(float(rec[metric]))

    strata_values: dict[tuple[str, int], np.ndarray] = {
        k: np.asarray(v, dtype=float) for k, v in by_stratum.items() if len(v) > 0
    }
    if not strata_values:
        return float("nan"), float("nan"), float("nan"), 0

    base_mean = float(np.mean([float(np.mean(v)) for v in strata_values.values()]))
    rng = np.random.default_rng(seed)
    draws = np.empty(bootstrap_n, dtype=float)
    strata = list(strata_values.values())

    for b in range(bootstrap_n):
        sampled_means: list[float] = []
        for arr in strata:
            n = arr.size
            idx = rng.integers(0, n, size=n)
            sampled_means.append(float(np.mean(arr[idx])))
        draws[b] = float(np.mean(sampled_means))

    ci_low, ci_high = quantile_ci(draws)
    return base_mean, ci_low, ci_high, len(strata)


def _attach_probability_and_hierarchical_fields(
    rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    group_cols: list[str],
    config: SimulationConfig,
    profile: SweepProfile,
) -> None:
    grouped = _group_records_by_keys(records, group_cols)
    mut_target = float(config.mutation_reduction_target)

    for row in rows:
        key = tuple(row[col] for col in group_cols)
        recs = grouped.get(key, [])
        if not recs:
            continue

        mut = np.asarray([float(r["mutation_reduction"]) for r in recs], dtype=float)
        via = np.asarray([float(r["viability"]) for r in recs], dtype=float)
        sphase = np.asarray([float(r["sphase_fold"]) for r in recs], dtype=float)
        leak = np.asarray([float(r["leakage"]) for r in recs], dtype=float)

        n = int(mut.size)
        mut_hit = int(np.sum(mut >= mut_target))
        via_hit = int(np.sum(via >= config.viability_threshold))
        joint_hit = int(np.sum((mut >= mut_target) & (via >= config.viability_threshold)))
        fucci_hit = int(np.sum((sphase >= config.fucci_threshold) & (leak <= config.fucci_leakage_cap)))

        row["pct_mut_ge_target"] = 100.0 * mut_hit / n
        # Backward-compatible alias; semantics follow mutation_reduction_target.
        row["pct_mut_ge_2"] = row["pct_mut_ge_target"]
        row["pct_viability_ge_threshold"] = 100.0 * via_hit / n
        row["pct_joint_mutation_viability"] = 100.0 * joint_hit / n
        row["pct_fucci_pass"] = 100.0 * fucci_hit / n

        mut_ci = proportion_ci_wilson(mut_hit, n)
        via_ci = proportion_ci_wilson(via_hit, n)
        joint_ci = proportion_ci_wilson(joint_hit, n)
        fucci_ci = proportion_ci_wilson(fucci_hit, n)

        row["pct_mut_ge_target_ci_low"] = 100.0 * mut_ci[0]
        row["pct_mut_ge_target_ci_high"] = 100.0 * mut_ci[1]
        row["pct_mut_ge_2_ci_low"] = row["pct_mut_ge_target_ci_low"]
        row["pct_mut_ge_2_ci_high"] = row["pct_mut_ge_target_ci_high"]
        row["pct_viability_ge_threshold_ci_low"] = 100.0 * via_ci[0]
        row["pct_viability_ge_threshold_ci_high"] = 100.0 * via_ci[1]
        row["pct_joint_mutation_viability_ci_low"] = 100.0 * joint_ci[0]
        row["pct_joint_mutation_viability_ci_high"] = 100.0 * joint_ci[1]
        row["pct_fucci_pass_ci_low"] = 100.0 * fucci_ci[0]
        row["pct_fucci_pass_ci_high"] = 100.0 * fucci_ci[1]

        # Hierarchical CIs are only meaningful when aggregating across donor/tissue strata.
        if "tissue" not in group_cols or "donor_age" not in group_cols:
            for metric in ("mutation_reduction", "viability", "sphase_fold", "leakage"):
                mean, ci_low, ci_high, n_strata = _hierarchical_bootstrap_ci(
                    recs=recs,
                    metric=metric,
                    bootstrap_n=min(160, max(80, int(profile.bootstrap_n))),
                    seed=deterministic_seed(
                        config.seed,
                        profile.name,
                        "hier",
                        metric,
                        key,
                    ),
                )
                row[f"{metric}_hier_mean"] = mean
                row[f"{metric}_hier_ci_low"] = ci_low
                row[f"{metric}_hier_ci_high"] = ci_high
                row[f"{metric}_hier_n_strata"] = n_strata


def _select_top_doses(config: SimulationConfig, coarse_dose_summary: list[dict[str, Any]]) -> list[DosePair]:
    if not coarse_dose_summary:
        return []

    feasible = [
        row for row in coarse_dose_summary if row["viability_ci_low"] >= config.viability_threshold
    ]
    if not feasible:
        feasible = coarse_dose_summary

    ranked = sorted(
        feasible,
        key=lambda r: r["mutation_reduction_mean"]
        * logistic_synergy(float(r["amplification"]), float(r["cyclin_throttle"]), config.objective.synergy_k),
        reverse=True,
    )

    return [
        DosePair(float(row["amplification"]), float(row["cyclin_throttle"]))
        for row in ranked[: config.dose_ranges.refine.top_k]
    ]


def _compute_washout_summary(
    raw_records: list[dict[str, Any]],
    washout_r2_floor: float,
    retention_horizon_divisions: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[float]]] = {}
    for rec in raw_records:
        tissue = str(rec["tissue"])
        bucket = grouped.setdefault(
            tissue,
            {
                "on_half_life": [],
                "off_half_life": [],
                "on_r2": [],
                "off_r2": [],
                "on_predictive_median_abs_log_error": [],
                "off_predictive_median_abs_log_error": [],
                "on_predictive_coverage": [],
                "off_predictive_coverage": [],
                "on_retained_effect_at_horizon": [],
                "off_retained_effect_at_horizon": [],
                "on_selected_model": [],
                "off_selected_model": [],
            },
        )
        bucket["on_half_life"].append(float(rec["on_half_life"]))
        bucket["off_half_life"].append(float(rec["off_half_life"]))
        bucket["on_r2"].append(float(rec["on_r2"]))
        bucket["off_r2"].append(float(rec["off_r2"]))
        bucket["on_predictive_median_abs_log_error"].append(float(rec["on_predictive_median_abs_log_error"]))
        bucket["off_predictive_median_abs_log_error"].append(float(rec["off_predictive_median_abs_log_error"]))
        bucket["on_predictive_coverage"].append(float(rec["on_predictive_coverage"]))
        bucket["off_predictive_coverage"].append(float(rec["off_predictive_coverage"]))
        bucket["on_retained_effect_at_horizon"].append(float(rec["on_retained_effect_at_horizon"]))
        bucket["off_retained_effect_at_horizon"].append(float(rec["off_retained_effect_at_horizon"]))
        bucket["on_selected_model"].append(str(rec["on_selected_model"]))
        bucket["off_selected_model"].append(str(rec["off_selected_model"]))

    rows: list[dict[str, Any]] = []
    for tissue, vals in grouped.items():
        for mark in ("on", "off"):
            hl_col = f"{mark}_half_life"
            r2_col = f"{mark}_r2"
            err_col = f"{mark}_predictive_median_abs_log_error"
            cov_col = f"{mark}_predictive_coverage"
            ret_col = f"{mark}_retained_effect_at_horizon"
            model_col = f"{mark}_selected_model"
            stats = summarize(vals[hl_col])
            err_stats = summarize(vals[err_col])
            cov_stats = summarize(vals[cov_col])
            ret_stats = summarize(vals[ret_col])
            models = vals[model_col]
            model_counts: dict[str, int] = {}
            for model in models:
                model_counts[model] = model_counts.get(model, 0) + 1
            rows.append(
                {
                    "tissue": tissue,
                    "mark": mark,
                    "half_life_mean": stats["mean"],
                    "half_life_ci_low": stats["ci_low"],
                    "half_life_ci_high": stats["ci_high"],
                    "half_life_std": stats["std"],
                    "r2_mean": float(np.mean(np.asarray(vals[r2_col], dtype=float))),
                    "finite_fraction": finite_fraction(vals[hl_col]),
                    "pct_r2_ge_floor": 100.0
                    * float(np.mean(np.asarray(vals[r2_col], dtype=float) >= washout_r2_floor)),
                    "predictive_median_abs_log_error_mean": err_stats["mean"],
                    "predictive_median_abs_log_error_median": err_stats["median"],
                    "predictive_median_abs_log_error_ci_low": err_stats["ci_low"],
                    "predictive_median_abs_log_error_ci_high": err_stats["ci_high"],
                    "predictive_coverage_mean": cov_stats["mean"],
                    "predictive_coverage_median": cov_stats["median"],
                    "predictive_coverage_ci_low": cov_stats["ci_low"],
                    "predictive_coverage_ci_high": cov_stats["ci_high"],
                    "retained_effect_mean": ret_stats["mean"],
                    "retained_effect_ci_low": ret_stats["ci_low"],
                    "retained_effect_ci_high": ret_stats["ci_high"],
                    "retained_effect_finite_fraction": finite_fraction(vals[ret_col]),
                    "retention_horizon_divisions": float(retention_horizon_divisions),
                    "selected_model_mode": max(model_counts.items(), key=lambda kv: kv[1])[0]
                    if model_counts
                    else "",
                    "selected_model_counts": model_counts,
                }
            )

    rows.sort(key=lambda r: (r["tissue"], r["mark"]))
    return rows


def _compute_best_dose_per_tissue(
    tissue_dose_summary: list[dict[str, Any]],
    viability_threshold: float,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in tissue_dose_summary:
        grouped.setdefault(str(row["tissue"]), []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for tissue, rows in grouped.items():
        feasible = [r for r in rows if r["viability_ci_low"] >= viability_threshold]
        target = feasible if feasible else rows
        if not target:
            continue
        best = sorted(target, key=lambda r: r["mutation_reduction_ci_low"], reverse=True)[0]
        out[tissue] = {
            "amplification": float(best["amplification"]),
            "cyclin_throttle": float(best["cyclin_throttle"]),
            "mutation_reduction_mean": float(best["mutation_reduction_mean"]),
            "mutation_reduction_ci": [
                float(best["mutation_reduction_ci_low"]),
                float(best["mutation_reduction_ci_high"]),
            ],
            "viability_ci_low": float(best["viability_ci_low"]),
            "sphase_fold_ci_low": float(best["sphase_fold_ci_low"]),
            "leakage_ci_high": float(best["leakage_ci_high"]),
        }

    return out


def _metric_value(row: dict[str, Any], metric: str, fallback: str) -> float:
    value = row.get(metric)
    if value is not None and np.isfinite(value):
        return float(value)
    return float(row.get(fallback, float("nan")))


def _pareto_frontier(rows: list[dict[str, Any]], x_key: str, y_key: str) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for row in rows:
        x = float(row[x_key])
        y = float(row[y_key])
        dominated = False
        for other in rows:
            ox = float(other[x_key])
            oy = float(other[y_key])
            if (ox >= x and oy >= y) and (ox > x or oy > y):
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    frontier.sort(key=lambda r: (float(r[x_key]), float(r[y_key])))
    return frontier


def _compute_pareto_band(dose_summary: list[dict[str, Any]], viability_threshold: float) -> dict[str, Any]:
    if not dose_summary:
        return {
            "has_feasible_region": False,
            "best_feasible_dose": None,
            "frontier_rows": [],
            "pareto_band_rows": [],
            "recommended_local_scan": None,
        }

    enriched: list[dict[str, Any]] = []
    for row in dose_summary:
        item = dict(row)
        item["mutation_score"] = _metric_value(row, "mutation_reduction_hier_mean", "mutation_reduction_mean")
        item["viability_score"] = _metric_value(row, "viability_hier_mean", "viability_mean")
        enriched.append(item)

    feasible = [r for r in enriched if float(r.get("viability_ci_low", float("nan"))) >= viability_threshold]
    if not feasible:
        return {
            "has_feasible_region": False,
            "best_feasible_dose": None,
            "frontier_rows": [],
            "pareto_band_rows": [],
            "recommended_local_scan": None,
        }

    frontier = _pareto_frontier(feasible, x_key="mutation_score", y_key="viability_score")
    best = max(feasible, key=lambda r: float(r.get("mutation_reduction_ci_low", float("-inf"))))

    best_mut = float(best["mutation_score"])
    band = [
        r
        for r in feasible
        if float(r["mutation_score"]) >= 0.95 * best_mut and float(r["viability_score"]) >= viability_threshold
    ]
    band.sort(key=lambda r: (float(r["amplification"]), float(r["cyclin_throttle"])))

    amps = sorted({float(r["amplification"]) for r in dose_summary})
    cyc = sorted({float(r["cyclin_throttle"]) for r in dose_summary})
    best_a = float(best["amplification"])
    best_c = float(best["cyclin_throttle"])

    def _step(axis: list[float], center: float) -> float:
        if len(axis) <= 1:
            return 0.0
        idx = int(np.argmin(np.abs(np.asarray(axis, dtype=float) - center)))
        if idx == 0:
            return axis[1] - axis[0]
        return axis[idx] - axis[idx - 1]

    a_step = _step(amps, best_a)
    c_step = _step(cyc, best_c)
    recommended_scan = {
        "center": {"amplification": best_a, "cyclin_throttle": best_c},
        "amplification_range": [max(min(amps), best_a - 2 * a_step), min(max(amps), best_a + 2 * a_step)],
        "cyclin_throttle_range": [max(min(cyc), best_c - 2 * c_step), min(max(cyc), best_c + 2 * c_step)],
        "suggested_grid_points": 5,
    }

    return {
        "has_feasible_region": True,
        "best_feasible_dose": {
            "amplification": best_a,
            "cyclin_throttle": best_c,
            "mutation_reduction_ci_low": float(best["mutation_reduction_ci_low"]),
            "viability_ci_low": float(best["viability_ci_low"]),
            "mutation_score": float(best["mutation_score"]),
            "viability_score": float(best["viability_score"]),
        },
        "frontier_rows": frontier,
        "pareto_band_rows": band,
        "recommended_local_scan": recommended_scan,
    }


def _compute_synergy(
    dose_summary: list[dict[str, Any]],
    config: SimulationConfig,
    profile: SweepProfile,
) -> dict[str, Any]:
    if not dose_summary:
        return {
            "has_feasible_optimum": False,
            "best_dose": None,
            "best_objective": None,
            "best_objective_ci": None,
            "bootstrap_stability": 0.0,
        }

    enriched: list[dict[str, Any]] = []
    for row in dose_summary:
        synergy_factor = logistic_synergy(
            float(row["amplification"]),
            float(row["cyclin_throttle"]),
            config.objective.synergy_k,
        )
        objective_mean = float(row["mutation_reduction_mean"] * synergy_factor)
        objective_sem = float(
            sem(float(row["mutation_reduction_std"]), int(row["mutation_reduction_n"])) * synergy_factor
        )
        item = dict(row)
        item["synergy_factor"] = synergy_factor
        item["objective_mean"] = objective_mean
        item["objective_sem"] = objective_sem
        enriched.append(item)

    feasible = [row for row in enriched if row["viability_ci_low"] >= config.viability_threshold]
    if not feasible:
        return {
            "has_feasible_optimum": False,
            "best_dose": None,
            "best_objective": None,
            "best_objective_ci": None,
            "bootstrap_stability": 0.0,
        }

    best = sorted(feasible, key=lambda r: r["objective_mean"], reverse=True)[0]

    rng = np.random.default_rng(deterministic_seed(config.seed, profile.name, "synergy-bootstrap"))
    winners: list[tuple[float, float]] = []
    for _ in range(profile.bootstrap_n):
        sampled = []
        for row in feasible:
            sampled_obj = float(
                rng.normal(
                    loc=float(row["objective_mean"]),
                    scale=max(float(row["objective_sem"]), 1e-6),
                )
            )
            sampled.append((sampled_obj, row))
        sampled.sort(key=lambda x: x[0], reverse=True)
        winners.append((float(sampled[0][1]["amplification"]), float(sampled[0][1]["cyclin_throttle"])))

    unique, counts = np.unique(np.asarray(winners), axis=0, return_counts=True)
    stability = float(counts.max() / counts.sum()) if counts.size else 0.0

    objective_table = sorted(
        [
            {
                "amplification": float(r["amplification"]),
                "cyclin_throttle": float(r["cyclin_throttle"]),
                "objective_mean": float(r["objective_mean"]),
                "objective_sem": float(r["objective_sem"]),
                "synergy_factor": float(r["synergy_factor"]),
                "viability_ci_low": float(r["viability_ci_low"]),
                "mutation_reduction_mean": float(r["mutation_reduction_mean"]),
            }
            for r in feasible
        ],
        key=lambda r: r["objective_mean"],
        reverse=True,
    )

    return {
        "has_feasible_optimum": True,
        "best_dose": {
            "amplification": float(best["amplification"]),
            "cyclin_throttle": float(best["cyclin_throttle"]),
        },
        "best_objective": float(best["objective_mean"]),
        "best_objective_ci": [
            float(best["objective_mean"] - 1.96 * max(best["objective_sem"], 1e-6)),
            float(best["objective_mean"] + 1.96 * max(best["objective_sem"], 1e-6)),
        ],
        "bootstrap_stability": stability,
        "objective_table": objective_table,
    }


def _build_tasks(config: SimulationConfig, profile: SweepProfile, doses: list[DosePair]) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for tissue in config.tissues:
        tissue_params = get_tissue_params(config, tissue)
        washout = get_washout_prior(config, tissue)
        for donor_age in config.donor_ages:
            mu0 = get_baseline_mutation_rate(config, tissue, donor_age)
            for dose in doses:
                seed = deterministic_seed(config.seed, profile.name, tissue, donor_age, dose.key())
                tasks.append(
                    TaskSpec(
                        tissue=tissue,
                        donor_age=donor_age,
                        amplification=float(dose.amplification),
                        cyclin_throttle=float(dose.cyclin_throttle),
                        replicates=profile.replicates_per_condition,
                        n_divisions=profile.n_divisions,
                        fucci_cells=profile.fucci_cells,
                        seed=seed,
                        mu0=mu0,
                        tissue_params=tissue_params,
                        washout_prior=washout,
                        washout_n_timepoints=config.washout_design.n_timepoints,
                        washout_cv_scale=config.washout_design.measurement_cv_scale,
                        washout_model_family=config.washout_design.model_family,
                        washout_holdout_fraction=config.washout_design.holdout_fraction,
                        washout_retention_horizon_divisions=config.washout_design.retention_horizon_divisions,
                        global_noise=config.global_noise,
                    )
                )
    return tasks


def _run_tasks(tasks: list[TaskSpec], workers: int) -> list[dict[str, Any]]:
    all_records: list[dict[str, Any]] = []
    if workers <= 1:
        for task in tasks:
            all_records.extend(_simulate_task(task))
        return all_records

    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_simulate_task, task) for task in tasks]
            for fut in as_completed(futures):
                all_records.extend(fut.result())
    except (PermissionError, OSError):  # pragma: no cover - environment dependent
        # Some restricted environments disable process semaphores. Fall back safely.
        for task in tasks:
            all_records.extend(_simulate_task(task))
    return all_records


def _drop_existing(doses: list[DosePair], existing: list[DosePair]) -> list[DosePair]:
    existing_set = {d.key() for d in existing}
    return [d for d in doses if d.key() not in existing_set]


def run_simulation(config: SimulationConfig, profile_override: str | None = None) -> SimulationRunResult:
    profile = config.resolved_profile(profile_override)
    workers = _resolve_workers(profile)

    coarse_doses = generate_coarse_doses(config)
    coarse_tasks = _build_tasks(config, profile, coarse_doses)
    coarse_raw = _run_tasks(coarse_tasks, workers=workers)

    coarse_dose_summary = _summarize_records(coarse_raw, ["amplification", "cyclin_throttle"])
    top_coarse_doses = _select_top_doses(config, coarse_dose_summary)

    refined_candidates = refine_doses(config, top_coarse_doses)
    refined_doses = _drop_existing(refined_candidates, coarse_doses)

    if refined_doses:
        refined_tasks = _build_tasks(config, profile, refined_doses)
        refined_raw = _run_tasks(refined_tasks, workers=workers)
        raw = coarse_raw + refined_raw
    else:
        raw = coarse_raw

    condition_summary = _summarize_records(raw, ["tissue", "donor_age", "amplification", "cyclin_throttle"])
    dose_summary = _summarize_records(raw, ["amplification", "cyclin_throttle"])
    tissue_dose_summary = _summarize_records(raw, ["tissue", "amplification", "cyclin_throttle"])
    _attach_probability_and_hierarchical_fields(
        rows=condition_summary,
        records=raw,
        group_cols=["tissue", "donor_age", "amplification", "cyclin_throttle"],
        config=config,
        profile=profile,
    )
    _attach_probability_and_hierarchical_fields(
        rows=dose_summary,
        records=raw,
        group_cols=["amplification", "cyclin_throttle"],
        config=config,
        profile=profile,
    )
    _attach_probability_and_hierarchical_fields(
        rows=tissue_dose_summary,
        records=raw,
        group_cols=["tissue", "amplification", "cyclin_throttle"],
        config=config,
        profile=profile,
    )

    washout_summary = _compute_washout_summary(
        raw,
        washout_r2_floor=config.washout_r2_floor,
        retention_horizon_divisions=config.washout_design.retention_horizon_divisions,
    )
    best_dose_per_tissue = _compute_best_dose_per_tissue(tissue_dose_summary, config.viability_threshold)
    pareto = _compute_pareto_band(dose_summary, viability_threshold=config.viability_threshold)
    synergy = _compute_synergy(dose_summary, config, profile)

    return SimulationRunResult(
        profile=profile.name,
        assumptions_hash=config.assumptions_hash(),
        raw_records=raw,
        condition_summary=condition_summary,
        dose_summary=dose_summary,
        tissue_dose_summary=tissue_dose_summary,
        washout_summary=washout_summary,
        best_dose_per_tissue=best_dose_per_tissue,
        pareto=pareto,
        synergy=synergy,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    # Keep deterministic order across records.
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_run_payload(result: SimulationRunResult, config: SimulationConfig, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    thresholds = {
        "viability_threshold": config.viability_threshold,
        "mutation_reduction_target": config.mutation_reduction_target,
        "fucci_threshold": config.fucci_threshold,
        "fucci_leakage_cap": config.fucci_leakage_cap,
        "washout_r2_floor": config.washout_r2_floor,
        "synergy_stability_min": config.synergy_stability_min,
        "strict_gate_require_all_tissues": config.strict_gate.require_all_tissues,
        "strict_gate_require_all_tissue_age": config.strict_gate.require_all_tissue_age,
        "washout_design_n_timepoints": config.washout_design.n_timepoints,
        "washout_design_cv_scale": config.washout_design.measurement_cv_scale,
        "washout_model_family": config.washout_design.model_family,
        "washout_holdout_fraction": config.washout_design.holdout_fraction,
        "washout_retention_horizon_divisions": config.washout_design.retention_horizon_divisions,
        "washout_retention_min": config.washout_design.retention_min,
        "washout_predictive_metric": config.washout_design.predictive_metric,
        "washout_predictive_mae_max": config.washout_design.predictive_mae_max,
        "washout_predictive_coverage_min": config.washout_design.predictive_coverage_min,
        "washout_gate_off_mark_required": config.washout_design.gate_off_mark_required,
    }

    metrics_json = result.to_metrics_json(thresholds=thresholds, config_source=str(config.source_path))

    (out_dir / "metrics_summary.json").write_text(json.dumps(metrics_json, indent=2), encoding="utf-8")
    _write_csv(out_dir / "raw_records.csv", result.raw_records)
    _write_csv(out_dir / "condition_summary.csv", result.condition_summary)
    _write_csv(out_dir / "dose_summary.csv", result.dose_summary)
    _write_csv(out_dir / "washout_summary.csv", result.washout_summary)

    return metrics_json
