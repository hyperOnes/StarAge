from __future__ import annotations

from typing import Any

import numpy as np


def _ci_value(row: dict[str, Any], metric: str, side: str) -> float:
    """Prefer hierarchical CI if present, otherwise fall back to pooled CI."""
    hier_key = f"{metric}_hier_ci_{side}"
    base_key = f"{metric}_ci_{side}"
    value = row.get(hier_key)
    if value is not None and np.isfinite(value):
        return float(value)
    return float(row.get(base_key, float("nan")))


def _row_with_max(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda r: float(r.get(key, float("-inf"))))


def _dose_key(row: dict[str, Any]) -> tuple[float, float]:
    return (float(row["amplification"]), float(row["cyclin_throttle"]))


def _dose_filter(rows: list[dict[str, Any]], key: tuple[float, float]) -> list[dict[str, Any]]:
    a, c = key
    out = []
    for row in rows:
        if abs(float(row["amplification"]) - a) < 1e-12 and abs(float(row["cyclin_throttle"]) - c) < 1e-12:
            out.append(row)
    return out


def _row_mutation_viability_pass(
    row: dict[str, Any], viability_threshold: float, mutation_target: float
) -> bool:
    return _ci_value(row, "mutation_reduction", "low") >= mutation_target and _ci_value(
        row, "viability", "low"
    ) >= viability_threshold


def evaluate_gate(metrics: dict[str, Any]) -> dict[str, Any]:
    thresholds = metrics["thresholds"]
    dose_summary: list[dict[str, Any]] = metrics["dose_summary"]
    tissue_dose_summary: list[dict[str, Any]] = metrics.get("tissue_dose_summary", [])
    condition_summary: list[dict[str, Any]] = metrics.get("condition_summary", [])
    washout_summary: list[dict[str, Any]] = metrics["washout_summary"]
    pareto = metrics.get("pareto", {})
    synergy = metrics["synergy"]

    viability_threshold = float(thresholds["viability_threshold"])
    mutation_target = float(thresholds.get("mutation_reduction_target", 1.80))
    fucci_threshold = float(thresholds["fucci_threshold"])
    fucci_leakage_cap = float(thresholds["fucci_leakage_cap"])
    washout_r2_floor = float(thresholds.get("washout_r2_floor", 0.90))
    washout_retention_horizon = float(thresholds.get("washout_retention_horizon_divisions", 80.0))
    washout_retention_min = float(thresholds.get("washout_retention_min", 0.70))
    washout_predictive_metric = str(thresholds.get("washout_predictive_metric", "predictive_coverage"))
    washout_predictive_mae_max = float(thresholds.get("washout_predictive_mae_max", 0.20))
    washout_predictive_coverage_min = float(thresholds.get("washout_predictive_coverage_min", 0.80))
    washout_gate_off_mark_required = bool(thresholds.get("washout_gate_off_mark_required", False))
    synergy_stability_min = float(thresholds["synergy_stability_min"])
    strict_require_all_tissues = bool(thresholds.get("strict_gate_require_all_tissues", True))
    strict_require_all_tissue_age = bool(thresholds.get("strict_gate_require_all_tissue_age", False))

    mutation_candidates: list[dict[str, Any]] = []
    fucci_candidates: list[dict[str, Any]] = []

    for row in dose_summary:
        mut_ci_low = _ci_value(row, "mutation_reduction", "low")
        via_ci_low = _ci_value(row, "viability", "low")
        s_ci_low = _ci_value(row, "sphase_fold", "low")
        l_ci_high = _ci_value(row, "leakage", "high")

        if mut_ci_low >= mutation_target and via_ci_low >= viability_threshold:
            mutation_candidates.append(row)
        if s_ci_low >= fucci_threshold and l_ci_high <= fucci_leakage_cap:
            fucci_candidates.append(row)

    strict_candidates: list[dict[str, Any]] = []
    strict_failures_by_dose: dict[str, Any] = {}
    if strict_require_all_tissues and tissue_dose_summary:
        for candidate in mutation_candidates:
            key = _dose_key(candidate)
            tissue_rows = _dose_filter(tissue_dose_summary, key)
            failing_tissues = [
                row for row in tissue_rows if not _row_mutation_viability_pass(row, viability_threshold, mutation_target)
            ]
            failing_tissue_age: list[dict[str, Any]] = []
            if strict_require_all_tissue_age and condition_summary:
                cond_rows = _dose_filter(condition_summary, key)
                failing_tissue_age = [
                    row
                    for row in cond_rows
                    if not _row_mutation_viability_pass(row, viability_threshold, mutation_target)
                ]
            if not failing_tissues and not failing_tissue_age:
                strict_candidates.append(candidate)
            strict_failures_by_dose[f"A={key[0]:.6g},C={key[1]:.6g}"] = {
                "failing_tissues": failing_tissues,
                "failing_tissue_age": failing_tissue_age,
            }

    mutation_pass = len(strict_candidates) > 0 if strict_require_all_tissues else len(mutation_candidates) > 0
    fucci_pass = len(fucci_candidates) > 0

    best_mutation_any = _row_with_max(dose_summary, "mutation_reduction_ci_low")
    feasible_rows = [row for row in dose_summary if _ci_value(row, "viability", "low") >= viability_threshold]
    best_feasible = _row_with_max(feasible_rows, "mutation_reduction_ci_low")

    mutation_confidence_pct = 0.0
    selected_mutation_row = strict_candidates[0] if (strict_require_all_tissues and strict_candidates) else None
    if selected_mutation_row is not None:
        key = _dose_key(selected_mutation_row)
        tissue_rows = _dose_filter(tissue_dose_summary, key)
        if strict_require_all_tissue_age and condition_summary:
            cond_rows = _dose_filter(condition_summary, key)
            if cond_rows:
                mutation_confidence_pct = min(float(r.get("pct_joint_mutation_viability", 0.0)) for r in cond_rows)
        elif tissue_rows:
            mutation_confidence_pct = min(float(r.get("pct_joint_mutation_viability", 0.0)) for r in tissue_rows)
        else:
            mutation_confidence_pct = float(selected_mutation_row.get("pct_joint_mutation_viability", 0.0))
    elif best_feasible is not None:
        mutation_confidence_pct = float(best_feasible.get("pct_joint_mutation_viability", 0.0))
    elif best_mutation_any is not None:
        mutation_confidence_pct = float(best_mutation_any.get("pct_joint_mutation_viability", 0.0))

    mutation_margin_pct = None
    if best_feasible is not None:
        mutation_margin_pct = (
            (_ci_value(best_feasible, "mutation_reduction", "low") / mutation_target) - 1.0
        ) * 100.0

    viability_margin_pct = None
    if best_mutation_any is not None:
        viability_margin_pct = (
            (_ci_value(best_mutation_any, "viability", "low") / viability_threshold) - 1.0
        ) * 100.0

    if mutation_pass and strict_require_all_tissues:
        mutation_explanation = (
            "A single dose pair satisfies lower-95 mutation and viability thresholds across all targeted tissues"
            + (" and donor-age strata." if strict_require_all_tissue_age else ".")
        )
    elif mutation_pass:
        mutation_explanation = "A dose pair meets lower-95 mutation reduction and viability thresholds simultaneously."
    elif best_mutation_any is None:
        mutation_explanation = "No dose results were available for mutation evaluation."
    elif strict_require_all_tissues and mutation_candidates and not strict_candidates:
        mutation_explanation = (
            "Some dose pairs pass global mutation+viability thresholds, but at least one tissue"
            + (" or tissue-age stratum" if strict_require_all_tissue_age else "")
            + " fails under strict finish-line criteria."
        )
    elif _ci_value(best_mutation_any, "mutation_reduction", "low") >= mutation_target and _ci_value(
        best_mutation_any, "viability", "low"
    ) < viability_threshold:
        mutation_explanation = (
            "High-dose settings satisfy mutation threshold, but fail viability threshold; outcome is viability-limited."
        )
    else:
        mutation_explanation = (
            f"Within viable dose settings, lower-95 mutation reduction remains below the {mutation_target:.3g} threshold."
        )

    best_fucci = _row_with_max(dose_summary, "pct_fucci_pass")
    fucci_confidence_pct = float(best_fucci.get("pct_fucci_pass", 0.0)) if best_fucci else 0.0
    fucci_explanation = (
        "S-phase fold and leakage meet thresholds at one or more dose pairs."
        if fucci_pass
        else "No dose pair met both S-phase fold and leakage constraints."
    )

    required_marks = {"on", "off"} if washout_gate_off_mark_required else {"on"}
    required_rows = [row for row in washout_summary if str(row.get("mark", "")) in required_marks]
    optional_rows = [row for row in washout_summary if str(row.get("mark", "")) not in required_marks]

    washout_failures: list[dict[str, Any]] = []
    washout_pass_rows: list[dict[str, Any]] = []
    r2_qc_below_floor: list[dict[str, Any]] = []
    for row in required_rows:
        reasons: list[str] = []
        finite_ci = np.isfinite(float(row.get("half_life_ci_low", float("nan")))) and np.isfinite(
            float(row.get("half_life_ci_high", float("nan")))
        )
        if not finite_ci:
            reasons.append("half_life_ci_not_finite")
        if float(row.get("finite_fraction", 0.0)) < 0.95:
            reasons.append("finite_fraction_below_0.95")

        retained_ci_low = float(row.get("retained_effect_ci_low", float("nan")))
        if not np.isfinite(retained_ci_low) or retained_ci_low < washout_retention_min:
            reasons.append("retained_effect_ci_low_below_threshold")

        predictive_ok = False
        predictive_value = float("nan")
        if washout_predictive_metric == "median_abs_log_error":
            predictive_value = float(
                row.get(
                    "predictive_median_abs_log_error_median",
                    row.get("predictive_median_abs_log_error_mean", float("nan")),
                )
            )
            predictive_ok = np.isfinite(predictive_value) and predictive_value <= washout_predictive_mae_max
            if not predictive_ok:
                reasons.append("predictive_median_abs_log_error_above_threshold")
        else:
            predictive_value = float(
                row.get("predictive_coverage_mean", row.get("predictive_coverage_median", float("nan")))
            )
            predictive_ok = np.isfinite(predictive_value) and predictive_value >= washout_predictive_coverage_min
            if not predictive_ok:
                reasons.append("predictive_coverage_below_threshold")

        if float(row.get("r2_mean", 0.0)) < washout_r2_floor:
            r2_qc_below_floor.append(row)

        if reasons:
            failed = dict(row)
            failed["failure_reasons"] = reasons
            failed["predictive_metric"] = washout_predictive_metric
            failed["predictive_value_used"] = predictive_value
            washout_failures.append(failed)
        else:
            washout_pass_rows.append(row)

    washout_pass = len(required_rows) > 0 and len(washout_failures) == 0
    washout_pass_fraction_pct = 100.0 * len(washout_pass_rows) / len(required_rows) if required_rows else 0.0

    if washout_pass:
        washout_explanation = (
            "Required washout rows pass finite-CI, retention-at-horizon, and predictive holdout checks."
        )
    elif not required_rows:
        washout_explanation = "Washout summary was empty for required marks; milestone cannot be confirmed."
    else:
        worst = washout_failures[0]
        washout_explanation = (
            f"One or more required tissue/mark rows fail washout persistence/predictive checks "
            f"(example {worst.get('tissue')}:{worst.get('mark')} reasons={worst.get('failure_reasons')})."
        )

    synergy_pass = (
        bool(synergy.get("has_feasible_optimum", False))
        and float(synergy.get("bootstrap_stability", 0.0)) >= synergy_stability_min
    )
    synergy_confidence_pct = 100.0 * float(synergy.get("bootstrap_stability", 0.0))
    synergy_explanation = (
        "Bootstrap stability supports a consistent optimum under viability constraints."
        if synergy_pass
        else "Bootstrap stability is below required minimum for a stable synergistic optimum."
    )

    mutation_row_for_estimate = (
        strict_candidates[0] if (strict_require_all_tissues and strict_candidates) else (mutation_candidates[0] if mutation_candidates else None)
    )
    mutation_est = mutation_row_for_estimate["mutation_reduction_mean"] if mutation_row_for_estimate is not None else None
    mutation_ci = (
        [
            _ci_value(mutation_row_for_estimate, "mutation_reduction", "low"),
            _ci_value(mutation_row_for_estimate, "mutation_reduction", "high"),
        ]
        if mutation_row_for_estimate is not None
        else None
    )

    fucci_est = fucci_candidates[0]["sphase_fold_mean"] if fucci_pass else None
    fucci_ci = (
        [
            _ci_value(fucci_candidates[0], "sphase_fold", "low"),
            _ci_value(fucci_candidates[0], "sphase_fold", "high"),
        ]
        if fucci_pass
        else None
    )

    verdict = {
        "milestones": {
            "mutation": {
                "pass": mutation_pass,
                "estimate": mutation_est,
                "ci": mutation_ci,
                "confidence_pct": mutation_confidence_pct,
                "threshold_margin_pct": mutation_margin_pct,
                "viability_margin_pct_at_best_mutation_dose": viability_margin_pct,
                "explanation": mutation_explanation,
                "evidence": {
                    "passing_doses": mutation_candidates[:3],
                    "strict_passing_doses": strict_candidates[:3],
                    "best_feasible_dose": best_feasible,
                    "best_mutation_dose_any_viability": best_mutation_any,
                    "strict_failures_by_dose": strict_failures_by_dose,
                    "strict_require_all_tissues": strict_require_all_tissues,
                    "strict_require_all_tissue_age": strict_require_all_tissue_age,
                },
            },
            "fucci": {
                "pass": fucci_pass,
                "estimate": fucci_est,
                "ci": fucci_ci,
                "confidence_pct": fucci_confidence_pct,
                "explanation": fucci_explanation,
                "evidence": {
                    "passing_doses": fucci_candidates[:3],
                    "best_dose_by_fucci_pass_rate": best_fucci,
                },
            },
            "washout": {
                "pass": washout_pass,
                "estimate": float(np.mean([float(r.get("retained_effect_mean", float("nan"))) for r in required_rows]))
                if required_rows
                else None,
                "ci": [
                    float(np.nanmin([float(r.get("retained_effect_ci_low", float("nan"))) for r in required_rows])),
                    float(np.nanmax([float(r.get("retained_effect_ci_high", float("nan"))) for r in required_rows])),
                ]
                if required_rows
                else None,
                "confidence_pct": washout_pass_fraction_pct,
                "explanation": washout_explanation,
                "evidence": {
                    "required_marks": sorted(required_marks),
                    "required_passed_rows": washout_pass_rows,
                    "failed_rows": washout_failures,
                    "optional_qc_rows": optional_rows,
                    "r2_qc_below_floor_rows": r2_qc_below_floor,
                    "model_family": thresholds.get("washout_model_family"),
                    "n_timepoints": thresholds.get("washout_design_n_timepoints"),
                    "measurement_cv_scale": thresholds.get("washout_design_cv_scale"),
                    "holdout_fraction": thresholds.get("washout_holdout_fraction"),
                },
            },
            "synergy": {
                "pass": synergy_pass,
                "estimate": synergy.get("best_objective"),
                "ci": synergy.get("best_objective_ci"),
                "confidence_pct": synergy_confidence_pct,
                "explanation": synergy_explanation,
                "evidence": synergy,
            },
        },
    }

    verdict["global_tranche1_verdict"] = all(v["pass"] for v in verdict["milestones"].values())
    verdict["milestone_pass_rate_pct"] = (
        100.0
        * sum(1 for milestone in verdict["milestones"].values() if milestone["pass"])
        / len(verdict["milestones"])
    )
    verdict["best_dose_per_tissue"] = metrics.get("best_dose_per_tissue", {})
    verdict["pareto"] = pareto
    verdict["gate_definitions"] = {
        "mutation_rule": (
            f"exists dose: lower95(mutation_reduction)>={mutation_target:.3g} and "
            "lower95(viability)>=viability_threshold"
            + (" and all tissues meet same rule" if strict_require_all_tissues else "")
            + (" and all tissue-age strata meet same rule" if strict_require_all_tissue_age else "")
        ),
        "fucci_rule": "exists dose: lower95(sphase_fold)>=fucci_threshold and upper95(leakage)<=fucci_leakage_cap",
        "washout_rule": (
            "for each required tissue/mark (on-mark only unless gate_off_mark_required=true): "
            "finite half-life CI, finite_fraction>=0.95, "
            "lower95(retained_effect_at_horizon)>=washout_retention_min, and predictive holdout quality check"
        ),
        "synergy_rule": "has_feasible_optimum and bootstrap_stability>=synergy_stability_min",
        "thresholds": {
            "viability_threshold": viability_threshold,
            "mutation_reduction_target": mutation_target,
            "fucci_threshold": fucci_threshold,
            "fucci_leakage_cap": fucci_leakage_cap,
            "washout_retention_horizon_divisions": washout_retention_horizon,
            "washout_retention_min": washout_retention_min,
            "washout_predictive_metric": washout_predictive_metric,
            "washout_predictive_mae_max": washout_predictive_mae_max,
            "washout_predictive_coverage_min": washout_predictive_coverage_min,
            "washout_gate_off_mark_required": washout_gate_off_mark_required,
            "washout_r2_floor_qc_only": washout_r2_floor,
            "synergy_stability_min": synergy_stability_min,
            "strict_require_all_tissues": strict_require_all_tissues,
            "strict_require_all_tissue_age": strict_require_all_tissue_age,
            "washout_model_family": thresholds.get("washout_model_family"),
            "washout_n_timepoints": thresholds.get("washout_design_n_timepoints"),
            "washout_cv_scale": thresholds.get("washout_design_cv_scale"),
            "washout_holdout_fraction": thresholds.get("washout_holdout_fraction"),
        },
    }
    verdict["assumptions_hash"] = metrics.get("assumptions_hash")
    verdict["profile"] = metrics.get("profile")

    return verdict
