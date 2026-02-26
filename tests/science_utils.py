from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from starrage_sim.config import load_config
from starrage_sim.data.schemas import SCIENCE_DATASET_SCHEMAS
from starrage_sim.data.synthetic_generator import generate_synthetic_science_data


def write_science_config(
    path: Path,
    *,
    data_mode: str = "data_required",
    allow_synthetic_fallback: bool = False,
    science_enabled: bool = True,
) -> Path:
    test_mode = str(os.environ.get("STARRAGE_TEST_MODE", "fast")).strip().lower()
    deep_mode = test_mode == "deep"

    if deep_mode:
        quick_profile = {
            "replicates_per_condition": 12,
            "n_divisions": 24,
            "fucci_cells": 18,
            "bootstrap_n": 30,
            "workers": 1,
        }
        full_profile = {
            "replicates_per_condition": 20,
            "n_divisions": 30,
            "fucci_cells": 24,
            "bootstrap_n": 40,
            "workers": 1,
        }
        advi_steps = 2000
        nuts_draws = 300
        nuts_tune = 300
    else:
        quick_profile = {
            "replicates_per_condition": 8,
            "n_divisions": 20,
            "fucci_cells": 14,
            "bootstrap_n": 20,
            "workers": 1,
        }
        full_profile = {
            "replicates_per_condition": 14,
            "n_divisions": 24,
            "fucci_cells": 18,
            "bootstrap_n": 24,
            "workers": 1,
        }
        advi_steps = 600
        nuts_draws = 120
        nuts_tune = 120

    cfg = {
        "seed": 2027,
        "runtime_profile": "quick",
        "tissues": ["blood_cd34", "gut_lgr5"],
        "donor_ages": [25, 60],
        "dose_ranges": {
            "amplification": {"min": 1.0, "max": 1.4, "n_coarse": 2},
            "cyclin_throttle": {"min": 0.08, "max": 0.12, "n_coarse": 2},
            "refine": {"top_k": 1, "step_fraction": 0.5, "neighbor_radius": 1},
        },
        "washout_priors": {
            "blood_cd34": {"on_half_life": 80.0, "off_half_life": 140.0, "measurement_cv": 0.08},
            "gut_lgr5": {"on_half_life": 60.0, "off_half_life": 110.0, "measurement_cv": 0.10},
        },
        "viability_threshold": 0.80,
        "mutation_reduction_target": 1.80,
        "gate_rule": "confidence_bound_95",
        "fucci_threshold": 2.0,
        "fucci_leakage_cap": 0.45,
        "washout_r2_floor": 0.85,
        "synergy_stability_min": 0.35,
        "profiles": {
            "quick": quick_profile,
            "full": full_profile,
        },
        "tissue_params": {
            "blood_cd34": {
                "alpha": 1.0,
                "beta": 0.2,
                "v0": 0.97,
                "p1": 0.01,
                "p2": 0.075,
                "p3": 0.03,
                "p4": 0.03,
            },
            "gut_lgr5": {
                "alpha": 0.95,
                "beta": 0.18,
                "v0": 0.96,
                "p1": 0.012,
                "p2": 0.08,
                "p3": 0.03,
                "p4": 0.032,
            },
        },
        "baseline_mutation_rates": {
            "blood_cd34": {"25": 0.95, "60": 1.25},
            "gut_lgr5": {"25": 1.05, "60": 1.45},
        },
        "global_noise": {
            "mutation_noise_sigma": 0.1,
            "viability_noise_sigma": 0.01,
            "expression_noise_sigma": 0.08,
            "stress_scale": 0.5,
            "stress_penalty": 0.02,
            "leakage_baseline": 0.34,
            "leakage_noise_sigma": 0.03,
        },
        "objective": {"synergy_k": 2.0},
        "sensitivity_configs": [],
        "science_mode": {
            "enabled": science_enabled,
            "data_mode": data_mode,
        },
        "science_data": {
            "allow_synthetic_fallback": allow_synthetic_fallback,
        },
        "locus_panel": [
            {"tissue": "blood_cd34", "gene": "MSH2", "locus_id": "blood_msh2_l1"},
            {"tissue": "blood_cd34", "gene": "MLH1", "locus_id": "blood_mlh1_l1"},
            {"tissue": "gut_lgr5", "gene": "MSH2", "locus_id": "gut_msh2_l1"},
            {"tissue": "gut_lgr5", "gene": "MLH1", "locus_id": "gut_mlh1_l1"},
        ],
        "mechanism_models": {"enabled_families": ["M1", "M2", "M3", "M4"]},
        "inference": {
            "backend": "pymc",
            "vi_settings": {"advi_steps": advi_steps},
            "nuts_settings": {"draws": nuts_draws, "tune": nuts_tune, "target_accept": 0.9},
            "comparison_metric": "loo",
            "sensitivity_backend": "surrogate",
        },
        "claim_thresholds": {
            "posterior_probability": 0.70,
            "claim1": {"delta_methylation_min": 0.02, "min_rows_per_locus": 4},
            "claim2": {"ppc_error_max": 0.50},
            "claim3": {"effect_min": 0.01},
            "claim4": {"mutation_reduction_min": 0.01},
            "claim5": {"model_margin_min": 1.0, "stability_min": 0.45},
        },
        "verdict_policy": {
            "science_as_hard_blocker": False,
            "allow_synthetic_science_pass_for_dev": False,
        },
    }
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_science_datasets(data_dir: Path, datasets: dict[str, list[dict[str, Any]]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for key, schema in SCIENCE_DATASET_SCHEMAS.items():
        rows = datasets.get(key, [])
        _write_csv(data_dir / schema.filename, rows)


def build_real_dataset(config_path: Path, data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    config = load_config(config_path)
    datasets = generate_synthetic_science_data(config)
    write_science_datasets(data_dir, datasets)
    return datasets


def flatten_methylation_age_slope(datasets: dict[str, list[dict[str, Any]]]) -> None:
    grouped: dict[tuple[str, str, str, str], float] = {}
    for row in datasets["methylation_observations"]:
        key = (row["tissue"], row["gene"], row["locus_id"], row["condition"])
        grouped.setdefault(key, float(row["methyl_beta"]))
        row["methyl_beta"] = round(grouped[key], 6)


def remove_demethylation_uplift(datasets: dict[str, list[dict[str, Any]]]) -> None:
    baseline_map: dict[tuple[str, str, str], float] = {}
    for row in datasets["mmr_expression_observations"]:
        key = (row["donor_id"], row["gene"], row["phase"])
        if row["condition"] == "baseline":
            baseline_map[key] = float(row["expr_norm"])

    for row in datasets["mmr_expression_observations"]:
        if "demethyl" in str(row["condition"]):
            key = (row["donor_id"], row["gene"], row["phase"])
            if key in baseline_map:
                row["expr_norm"] = round(baseline_map[key] - 0.02, 6)


def enforce_alt_mechanism_profile(datasets: dict[str, list[dict[str, Any]]]) -> None:
    rng = np.random.default_rng(33)

    for row in datasets["e2f_activity_observations"]:
        age_norm = (float(row["age"]) - 25.0) / 35.0
        row["e2f_signal"] = round(float(0.9 + 0.45 * age_norm + rng.normal(0.0, 0.03)), 6)

    for row in datasets["mmr_expression_observations"]:
        phase = str(row["phase"])
        age_norm = (float(row["age"]) - 25.0) / 35.0
        row["expr_norm"] = round(float(0.7 + (0.95 if phase == "S" else 0.1) + 0.06 * age_norm + rng.normal(0.0, 0.03)), 6)

    for row in datasets["mutation_observations"]:
        age_norm = (float(row["age"]) - 25.0) / 35.0
        row["mutations_per_division"] = round(float(0.95 + 0.38 * age_norm + rng.normal(0.0, 0.02)), 6)


def increase_donor_variance(datasets: dict[str, list[dict[str, Any]]], sigma: float = 0.03, seed: int = 21) -> None:
    rng = np.random.default_rng(seed)
    donor_effect: dict[str, float] = {}

    for key in (
        "methylation_observations",
        "e2f_activity_observations",
        "mmr_expression_observations",
        "mutation_observations",
        "viability_observations",
    ):
        for row in datasets[key]:
            donor = str(row["donor_id"])
            donor_effect.setdefault(donor, float(rng.normal(0.0, sigma)))
            d = donor_effect[donor]
            if key == "methylation_observations":
                row["methyl_beta"] = round(float(np.clip(float(row["methyl_beta"]) + d, 0.0, 1.0)), 6)
            elif key == "e2f_activity_observations":
                row["e2f_signal"] = round(float(np.clip(float(row["e2f_signal"]) + d, 0.01, 5.0)), 6)
            elif key == "mmr_expression_observations":
                row["expr_norm"] = round(float(np.clip(float(row["expr_norm"]) + d, 0.01, 10.0)), 6)
            elif key == "mutation_observations":
                row["mutations_per_division"] = round(
                    float(np.clip(float(row["mutations_per_division"]) + d, 0.001, 5.0)),
                    6,
                )
            elif key == "viability_observations":
                row["viability"] = round(float(np.clip(float(row["viability"]) + 0.25 * d, 0.0, 1.0)), 6)


def increase_measurement_noise(datasets: dict[str, list[dict[str, Any]]], sigma: float = 0.12, seed: int = 22) -> None:
    rng = np.random.default_rng(seed)
    for row in datasets["e2f_activity_observations"]:
        row["e2f_signal"] = round(float(np.clip(float(row["e2f_signal"]) + rng.normal(0.0, sigma), 0.01, 6.0)), 6)
    for row in datasets["mmr_expression_observations"]:
        row["expr_norm"] = round(float(np.clip(float(row["expr_norm"]) + rng.normal(0.0, sigma), 0.01, 10.0)), 6)
    for row in datasets["mutation_observations"]:
        row["mutations_per_division"] = round(
            float(np.clip(float(row["mutations_per_division"]) + rng.normal(0.0, sigma), 0.001, 6.0)),
            6,
        )


def introduce_missingness(datasets: dict[str, list[dict[str, Any]]], frac: float = 0.30, seed: int = 23) -> None:
    rng = np.random.default_rng(seed)
    for key in (
        "methylation_observations",
        "e2f_activity_observations",
        "mmr_expression_observations",
        "mutation_observations",
        "viability_observations",
    ):
        rows = datasets[key]
        keep = []
        for row in rows:
            if rng.random() >= frac:
                keep.append(row)
        if keep:
            datasets[key] = keep

    # Keep perturbation data schema-valid but remove target overlap with required loci.
    perturb_rows = datasets.get("perturbation_events", [])
    kept_perturb: list[dict[str, Any]] = []
    for row in perturb_rows:
        if rng.random() >= frac:
            updated = dict(row)
            updated["target_locus"] = f"missing_{row.get('target_locus', 'locus')}"
            kept_perturb.append(updated)

    if not kept_perturb and perturb_rows:
        updated = dict(perturb_rows[0])
        updated["target_locus"] = f"missing_{perturb_rows[0].get('target_locus', 'locus')}"
        kept_perturb.append(updated)

    if kept_perturb:
        datasets["perturbation_events"] = kept_perturb


def weaken_methylation_age_slope(datasets: dict[str, list[dict[str, Any]]], factor: float = 0.25) -> None:
    baseline_ref: dict[tuple[str, str, str, str], float] = {}
    for row in datasets["methylation_observations"]:
        key = (row["tissue"], row["gene"], row["locus_id"], row["condition"])
        age = int(row["age"])
        if age == 25:
            baseline_ref[key] = float(row["methyl_beta"])

    for row in datasets["methylation_observations"]:
        key = (row["tissue"], row["gene"], row["locus_id"], row["condition"])
        base = baseline_ref.get(key, float(row["methyl_beta"]))
        current = float(row["methyl_beta"])
        row["methyl_beta"] = round(float(base + factor * (current - base)), 6)


def weaken_demethylation_uplift(datasets: dict[str, list[dict[str, Any]]], factor: float = 0.30) -> None:
    baseline_map: dict[tuple[str, str, str], float] = {}
    for row in datasets["mmr_expression_observations"]:
        key = (row["donor_id"], row["gene"], row["phase"])
        if row["condition"] == "baseline":
            baseline_map[key] = float(row["expr_norm"])

    for row in datasets["mmr_expression_observations"]:
        if "demethyl" in str(row["condition"]):
            key = (row["donor_id"], row["gene"], row["phase"])
            base = baseline_map.get(key, float(row["expr_norm"]))
            current = float(row["expr_norm"])
            row["expr_norm"] = round(float(base + factor * (current - base)), 6)
