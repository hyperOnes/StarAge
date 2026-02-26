from __future__ import annotations

import json
from pathlib import Path

from starrage_sim.config import load_config
from starrage_sim.engine.simulator import run_simulation
from starrage_sim.metrics.gate_eval import evaluate_gate


def _write_cfg(path: Path, alpha_scale: float) -> Path:
    config = {
        "seed": 1234,
        "runtime_profile": "quick",
        "tissues": ["blood_cd34", "gut_lgr5", "skin_stem"],
        "donor_ages": [25, 60],
        "dose_ranges": {
            "amplification": {"min": 0.8, "max": 2.4, "n_coarse": 2},
            "cyclin_throttle": {"min": 0.06, "max": 0.16, "n_coarse": 2},
            "refine": {"top_k": 1, "step_fraction": 0.5, "neighbor_radius": 1},
        },
        "washout_priors": {
            "blood_cd34": {"on_half_life": 80.0, "off_half_life": 150.0, "measurement_cv": 0.08},
            "gut_lgr5": {"on_half_life": 60.0, "off_half_life": 110.0, "measurement_cv": 0.10},
            "skin_stem": {"on_half_life": 90.0, "off_half_life": 160.0, "measurement_cv": 0.07},
        },
        "viability_threshold": 0.80,
        "mutation_reduction_target": 1.80,
        "gate_rule": "confidence_bound_95",
        "fucci_threshold": 2.0,
        "fucci_leakage_cap": 0.45,
        "washout_r2_floor": 0.85,
        "synergy_stability_min": 0.40,
        "profiles": {
            "quick": {
                "replicates_per_condition": 22,
                "n_divisions": 40,
                "fucci_cells": 36,
                "bootstrap_n": 80,
                "workers": 1,
            },
            "full": {
                "replicates_per_condition": 40,
                "n_divisions": 50,
                "fucci_cells": 45,
                "bootstrap_n": 100,
                "workers": 1,
            },
        },
        "tissue_params": {
            "blood_cd34": {
                "alpha": 1.0 * alpha_scale,
                "beta": 0.2,
                "v0": 0.97,
                "p1": 0.010,
                "p2": 0.075,
                "p3": 0.028,
                "p4": 0.030,
            },
            "gut_lgr5": {
                "alpha": 0.95 * alpha_scale,
                "beta": 0.18,
                "v0": 0.96,
                "p1": 0.011,
                "p2": 0.080,
                "p3": 0.030,
                "p4": 0.032,
            },
            "skin_stem": {
                "alpha": 0.9 * alpha_scale,
                "beta": 0.17,
                "v0": 0.965,
                "p1": 0.011,
                "p2": 0.082,
                "p3": 0.030,
                "p4": 0.033,
            },
        },
        "baseline_mutation_rates": {
            "blood_cd34": {"25": 0.9, "60": 1.3},
            "gut_lgr5": {"25": 1.1, "60": 1.6},
            "skin_stem": {"25": 1.0, "60": 1.5},
        },
        "global_noise": {
            "mutation_noise_sigma": 0.12,
            "viability_noise_sigma": 0.010,
            "expression_noise_sigma": 0.08,
            "stress_scale": 0.5,
            "stress_penalty": 0.02,
            "leakage_baseline": 0.34,
            "leakage_noise_sigma": 0.03,
        },
        "objective": {"synergy_k": 2.0},
        "sensitivity_configs": [],
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _run_and_gate(cfg_path: Path) -> dict:
    config = load_config(cfg_path)
    result = run_simulation(config, profile_override="quick")
    metrics = result.to_metrics_json(
        thresholds={
            "viability_threshold": config.viability_threshold,
            "mutation_reduction_target": config.mutation_reduction_target,
            "fucci_threshold": config.fucci_threshold,
            "fucci_leakage_cap": config.fucci_leakage_cap,
            "washout_r2_floor": config.washout_r2_floor,
            "synergy_stability_min": config.synergy_stability_min,
        },
        config_source=str(cfg_path),
    )
    return evaluate_gate(metrics)


def test_integration_known_pass(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "pass.json", alpha_scale=2.2)
    gate = _run_and_gate(cfg)
    assert gate["milestones"]["mutation"]["pass"]


def test_integration_known_fail(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path / "fail.json", alpha_scale=0.35)
    gate = _run_and_gate(cfg)
    assert not gate["milestones"]["mutation"]["pass"]
