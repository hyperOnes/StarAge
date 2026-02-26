from __future__ import annotations

import json
from pathlib import Path

from starrage_sim.config import load_config
from starrage_sim.engine.science_pipeline import run_science_fit
from starrage_sim.metrics.claims_eval import evaluate_claims
from starrage_sim.metrics.model_compare import compare_models
from tests.science_utils import (
    build_real_dataset,
    increase_donor_variance,
    increase_measurement_noise,
    introduce_missingness,
    weaken_demethylation_uplift,
    weaken_methylation_age_slope,
    write_science_config,
    write_science_datasets,
)


def _prepare_surrogate_cfg(cfg_path: Path) -> None:
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["inference"]["backend"] = "surrogate"
    raw["inference"]["sensitivity_backend"] = "surrogate"
    raw["runtime_profile"] = "quick"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")


def _run_claims(cfg_path: Path, data_dir: Path, out_dir: Path) -> dict:
    config = load_config(cfg_path)
    fit_payload = run_science_fit(config=config, out_dir=out_dir, data_dir=data_dir, runtime_profile="quick")
    compare_payload = compare_models(fit_payload, metric="loo")
    claims_payload = evaluate_claims(fit_payload=fit_payload, compare_payload=compare_payload, config=config)
    return claims_payload


def test_calibration_sensitivity_claim_behavior(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    _prepare_surrogate_cfg(cfg_path)

    base_dir = tmp_path / "base_data"
    base = build_real_dataset(cfg_path, base_dir)

    baseline_claims = _run_claims(cfg_path, base_dir, tmp_path / "out_base")
    assert baseline_claims["claims"]["1"]["status"] == "pass"
    assert baseline_claims["claims"]["3"]["status"] == "pass"

    donor_var = json.loads(json.dumps(base))
    increase_donor_variance(donor_var, sigma=0.03)
    donor_dir = tmp_path / "donor_var"
    write_science_datasets(donor_dir, donor_var)
    donor_claims = _run_claims(cfg_path, donor_dir, tmp_path / "out_donor_var")
    # Expected non-flip under moderate donor variance.
    assert donor_claims["claims"]["1"]["status"] == "pass"
    assert donor_claims["claims"]["5"]["status"] in {"pass", "fail"}

    noise = json.loads(json.dumps(base))
    increase_measurement_noise(noise, sigma=0.20)
    noise_dir = tmp_path / "noise"
    write_science_datasets(noise_dir, noise)
    noise_claims = _run_claims(cfg_path, noise_dir, tmp_path / "out_noise")
    assert noise_claims["claims"]["2"]["status"] == "fail"

    missing = json.loads(json.dumps(base))
    introduce_missingness(missing, frac=0.40)
    missing_dir = tmp_path / "missing"
    write_science_datasets(missing_dir, missing)
    missing_claims = _run_claims(cfg_path, missing_dir, tmp_path / "out_missing")
    assert missing_claims["claims"]["3"]["status"] == "fail"

    weak_slope = json.loads(json.dumps(base))
    weaken_methylation_age_slope(weak_slope, factor=0.0)
    weak_slope_dir = tmp_path / "weak_slope"
    write_science_datasets(weak_slope_dir, weak_slope)
    weak_slope_claims = _run_claims(cfg_path, weak_slope_dir, tmp_path / "out_weak_slope")
    assert weak_slope_claims["claims"]["1"]["status"] == "fail"

    weak_uplift = json.loads(json.dumps(base))
    weaken_demethylation_uplift(weak_uplift, factor=0.0)
    weak_uplift_dir = tmp_path / "weak_uplift"
    write_science_datasets(weak_uplift_dir, weak_uplift)
    weak_uplift_claims = _run_claims(cfg_path, weak_uplift_dir, tmp_path / "out_weak_uplift")
    assert weak_uplift_claims["claims"]["3"]["status"] == "fail"
