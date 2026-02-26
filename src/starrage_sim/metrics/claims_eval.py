from __future__ import annotations

from typing import Any

import numpy as np

from starrage_sim.config import SimulationConfig


VALID_STATUSES = {"pass", "fail", "insufficient_data", "synthetic_only_unvalidated"}


def _find_model(fit_payload: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    for model in fit_payload.get("models", []):
        if str(model.get("model_id")) == model_id:
            return model
    return None


def _probability(draws: list[float], predicate: Any) -> float:
    arr = np.asarray(draws, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("nan")
    return float(np.mean(predicate(arr)))


def _status_for_data_mode(has_real_data: bool, default_status: str) -> str:
    if not has_real_data:
        return "synthetic_only_unvalidated"
    return default_status


def evaluate_claims(
    fit_payload: dict[str, Any],
    compare_payload: dict[str, Any],
    config: SimulationConfig,
) -> dict[str, Any]:
    has_real_data = bool(fit_payload.get("has_real_data", False))
    posterior_cutoff = float(config.claim_thresholds.posterior_probability)
    m1 = _find_model(fit_payload, "M1")

    if m1 is None:
        empty_claims = {
            str(idx): {
                "status": "insufficient_data",
                "reason": "M1 fit not available",
            }
            for idx in range(1, 6)
        }
        return {
            "claims": empty_claims,
            "science_verdict": {
                "pass": False,
                "reason": "missing_primary_model",
            },
        }

    claim_inputs = m1.get("claim_inputs", {})
    parameters = m1.get("parameters", {})

    # Claim 1
    required_loci = {(loc.tissue, loc.gene, loc.locus_id) for loc in config.locus_panel}
    by_locus = {
        (str(r.get("tissue")), str(r.get("gene")), str(r.get("locus_id"))): r
        for r in claim_inputs.get("claim1_loci", [])
    }

    claim1_checks: list[dict[str, Any]] = []
    claim1_ok = True
    for locus_key in sorted(required_loci):
        row = by_locus.get(locus_key)
        if row is None:
            claim1_checks.append(
                {
                    "tissue": locus_key[0],
                    "gene": locus_key[1],
                    "locus_id": locus_key[2],
                    "status": "missing",
                }
            )
            claim1_ok = False
            continue

        row_count = int(row.get("row_count", 0))
        slope_prob = float(row.get("posterior_prob_age_slope_gt_0", float("nan")))
        delta = float(row.get("delta_methylation_25_to_60", float("nan")))

        row_ok = (
            row_count >= int(config.claim_thresholds.claim1_min_rows_per_locus)
            and np.isfinite(slope_prob)
            and slope_prob >= posterior_cutoff
            and np.isfinite(delta)
            and delta >= float(config.claim_thresholds.claim1_delta_methylation_min)
        )
        claim1_ok = claim1_ok and row_ok
        claim1_checks.append(
            {
                "tissue": locus_key[0],
                "gene": locus_key[1],
                "locus_id": locus_key[2],
                "row_count": row_count,
                "posterior_prob_age_slope_gt_0": slope_prob,
                "delta_methylation_25_to_60": delta,
                "row_pass": bool(row_ok),
            }
        )

    claim1_status = _status_for_data_mode(has_real_data, "pass" if claim1_ok else "fail")

    # Claim 2
    beta_methyl = parameters.get("beta_methyl_to_e2f", {})
    prob_neg = float(beta_methyl.get("p_lt_0", float("nan")))
    ppc_err = float(claim_inputs.get("claim2_ppc_error", m1.get("ppc_error", float("nan"))))
    claim2_ok = (
        np.isfinite(prob_neg)
        and prob_neg >= posterior_cutoff
        and np.isfinite(ppc_err)
        and ppc_err <= float(config.claim_thresholds.claim2_ppc_error_max)
    )
    claim2_status = _status_for_data_mode(has_real_data, "pass" if claim2_ok else "fail")

    # Claim 3
    events = fit_payload.get("validation_report", {}).get("datasets", [])
    perturb_dataset_present = any(
        item.get("dataset") == "perturbation_events"
        and item.get("status") == "valid"
        and item.get("row_count", 0) > 0
        for item in events
    )
    required_locus_ids = {loc.locus_id for loc in config.locus_panel}
    perturb_counts = fit_payload.get("channels", {}).get("counts", {})
    has_perturbation_rows = int(perturb_counts.get("perturbation", 0)) > 0
    perturb_targets = {str(x) for x in fit_payload.get("perturbation_targets", [])}
    target_overlap = len(required_locus_ids & perturb_targets) > 0 if required_locus_ids else False

    demethyl_draws = claim_inputs.get("claim3_effect_draws", [])
    prob_effect_gt0 = _probability(demethyl_draws, lambda arr: arr > 0.0)
    effect_mean = float(np.nanmean(np.asarray(demethyl_draws, dtype=float))) if demethyl_draws else float("nan")

    claim3_ok = (
        perturb_dataset_present
        and has_perturbation_rows
        and target_overlap
        and np.isfinite(prob_effect_gt0)
        and prob_effect_gt0 >= posterior_cutoff
        and np.isfinite(effect_mean)
        and effect_mean >= float(config.claim_thresholds.claim3_effect_min)
        and len(required_locus_ids) > 0
    )
    claim3_status = _status_for_data_mode(has_real_data, "pass" if claim3_ok else "fail")

    # Claim 4
    beta_mmr = parameters.get("beta_mmr_to_mutation", {})
    prob_beta_neg = float(beta_mmr.get("p_lt_0", float("nan")))
    effect_proxy = float(claim_inputs.get("claim4_effect_size_proxy", float("nan")))
    claim4_ok = (
        np.isfinite(prob_beta_neg)
        and prob_beta_neg >= posterior_cutoff
        and np.isfinite(effect_proxy)
        and effect_proxy >= float(config.claim_thresholds.claim4_mutation_reduction_min)
    )
    claim4_status = _status_for_data_mode(has_real_data, "pass" if claim4_ok else "fail")

    # Claim 5
    top_model = str(compare_payload.get("top_model")) if compare_payload.get("top_model") is not None else None
    margins = compare_payload.get("top_margin_vs_competitors", {})
    if top_model is None:
        claim5_status = "insufficient_data"
        claim5_ok = False
    elif not has_real_data:
        claim5_status = "synthetic_only_unvalidated"
        claim5_ok = False
    else:
        margin_ok = bool(margins) and all(
            float(value) >= float(config.claim_thresholds.claim5_model_margin_min)
            for value in margins.values()
            if np.isfinite(float(value))
        )

        stability = compare_payload.get("stability", {})
        lodo = float(stability.get("leave_one_donor_out", float("nan")))
        tissue = float(stability.get("by_tissue", float("nan")))
        stability_ok = (
            np.isfinite(lodo)
            and np.isfinite(tissue)
            and lodo >= float(config.claim_thresholds.claim5_stability_min)
            and tissue >= float(config.claim_thresholds.claim5_stability_min)
        )

        claim5_ok = top_model == "M1" and margin_ok and stability_ok
        claim5_status = "pass" if claim5_ok else "fail"

    claims = {
        "1": {
            "status": claim1_status,
            "criteria": {
                "posterior_prob_threshold": posterior_cutoff,
                "delta_methylation_min": float(config.claim_thresholds.claim1_delta_methylation_min),
                "min_rows_per_locus": int(config.claim_thresholds.claim1_min_rows_per_locus),
            },
            "details": {
                "required_locus_count": len(required_loci),
                "locus_checks": claim1_checks,
            },
        },
        "2": {
            "status": claim2_status,
            "criteria": {
                "posterior_prob_threshold": posterior_cutoff,
                "ppc_error_max": float(config.claim_thresholds.claim2_ppc_error_max),
            },
            "details": {
                "posterior_prob_beta_methyl_to_e2f_lt_0": prob_neg,
                "ppc_error": ppc_err,
            },
        },
        "3": {
            "status": claim3_status,
            "criteria": {
                "posterior_prob_threshold": posterior_cutoff,
                "effect_min": float(config.claim_thresholds.claim3_effect_min),
            },
            "details": {
                "perturbation_dataset_present": perturb_dataset_present,
                "targeted_loci_required": sorted(required_locus_ids),
                "targeted_loci_observed": sorted(perturb_targets),
                "posterior_prob_effect_gt_0": prob_effect_gt0,
                "effect_mean": effect_mean,
            },
        },
        "4": {
            "status": claim4_status,
            "criteria": {
                "posterior_prob_threshold": posterior_cutoff,
                "mutation_reduction_min": float(config.claim_thresholds.claim4_mutation_reduction_min),
            },
            "details": {
                "posterior_prob_beta_mmr_to_mutation_lt_0": prob_beta_neg,
                "effect_size_proxy": effect_proxy,
            },
        },
        "5": {
            "status": claim5_status,
            "criteria": {
                "required_top_model": "M1",
                "model_margin_min": float(config.claim_thresholds.claim5_model_margin_min),
                "stability_min": float(config.claim_thresholds.claim5_stability_min),
            },
            "details": {
                "top_model": top_model,
                "top_margin_vs_competitors": margins,
                "stability": compare_payload.get("stability", {}),
            },
        },
    }

    for key, value in claims.items():
        status = value["status"]
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid claim status for claim {key}: {status}")

    allow_synthetic_dev_pass = bool(config.verdict_policy.allow_synthetic_science_pass_for_dev)
    statuses = [claims[str(i)]["status"] for i in range(1, 6)]
    has_synthetic_unvalidated = any(status == "synthetic_only_unvalidated" for status in statuses)
    if allow_synthetic_dev_pass:
        science_pass = all(status in {"pass", "synthetic_only_unvalidated"} for status in statuses)
    else:
        science_pass = all(status == "pass" for status in statuses)

    if has_synthetic_unvalidated and not allow_synthetic_dev_pass:
        science_pass = False

    return {
        "claims": claims,
        "science_verdict": {
            "pass": bool(science_pass),
            "has_real_data": has_real_data,
            "mode": fit_payload.get("data_mode"),
            "top_model": compare_payload.get("top_model"),
            "has_synthetic_unvalidated_claims": has_synthetic_unvalidated,
            "allow_synthetic_science_pass_for_dev": allow_synthetic_dev_pass,
        },
    }
