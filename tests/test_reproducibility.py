from __future__ import annotations

import json
from pathlib import Path

from starrage_sim.config import load_config
from starrage_sim.engine.simulator import run_simulation


def _tiny_cfg(path: Path) -> Path:
    cfg = {
        "seed": 2026,
        "runtime_profile": "quick",
        "tissues": ["blood_cd34"],
        "donor_ages": [25],
        "dose_ranges": {
            "amplification": {"min": 1.0, "max": 2.0, "n_coarse": 2},
            "cyclin_throttle": {"min": 0.08, "max": 0.12, "n_coarse": 2},
            "refine": {"top_k": 1, "step_fraction": 0.5, "neighbor_radius": 1},
        },
        "washout_priors": {
            "blood_cd34": {"on_half_life": 80.0, "off_half_life": 140.0, "measurement_cv": 0.08}
        },
        "viability_threshold": 0.80,
        "mutation_reduction_target": 1.80,
        "gate_rule": "confidence_bound_95",
        "fucci_threshold": 2.0,
        "fucci_leakage_cap": 0.45,
        "washout_r2_floor": 0.85,
        "synergy_stability_min": 0.4,
        "profiles": {
            "quick": {
                "replicates_per_condition": 12,
                "n_divisions": 24,
                "fucci_cells": 20,
                "bootstrap_n": 25,
                "workers": 1,
            },
            "full": {
                "replicates_per_condition": 20,
                "n_divisions": 30,
                "fucci_cells": 30,
                "bootstrap_n": 40,
                "workers": 1,
            },
        },
        "tissue_params": {
            "blood_cd34": {
                "alpha": 1.1,
                "beta": 0.2,
                "v0": 0.97,
                "p1": 0.01,
                "p2": 0.075,
                "p3": 0.03,
                "p4": 0.03,
            }
        },
        "baseline_mutation_rates": {"blood_cd34": {"25": 1.0}},
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
    }
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _sort_rows(rows: list[dict], keys: list[str]) -> list[dict]:
    return sorted(rows, key=lambda r: tuple(r[k] for k in keys))


def test_deterministic_reproducibility(tmp_path: Path) -> None:
    cfg = load_config(_tiny_cfg(tmp_path / "tiny.json"))

    r1 = run_simulation(cfg, profile_override="quick")
    r2 = run_simulation(cfg, profile_override="quick")

    d1 = _sort_rows(r1.dose_summary, ["amplification", "cyclin_throttle"])
    d2 = _sort_rows(r2.dose_summary, ["amplification", "cyclin_throttle"])

    assert len(d1) == len(d2)
    cols = [
        "mutation_reduction_mean",
        "mutation_reduction_ci_low",
        "viability_mean",
        "viability_ci_low",
        "sphase_fold_mean",
        "sphase_fold_ci_low",
    ]

    for idx in range(len(d1)):
        for col in cols:
            assert d1[idx][col] == d2[idx][col]
