from __future__ import annotations

from pathlib import Path

from starrage_sim.config import load_config
from starrage_sim.engine.science_pipeline import run_science_fit
from starrage_sim.metrics.claims_eval import evaluate_claims
from starrage_sim.metrics.model_compare import compare_models
from tests.science_utils import (
    build_real_dataset,
    enforce_alt_mechanism_profile,
    flatten_methylation_age_slope,
    remove_demethylation_uplift,
    write_science_config,
    write_science_datasets,
)


def _fit_compare_claims(cfg_path: Path, data_dir: Path, out_dir: Path) -> tuple[dict, dict, dict]:
    config = load_config(cfg_path)
    fit_payload = run_science_fit(config=config, out_dir=out_dir, data_dir=data_dir)
    compare_payload = compare_models(fit_payload, metric="loo")
    claims_payload = evaluate_claims(fit_payload=fit_payload, compare_payload=compare_payload, config=config)
    return fit_payload, compare_payload, claims_payload


def test_sign_consistency_and_model_comparison_outputs(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    build_real_dataset(cfg_path, data_dir)

    fit_payload, compare_payload, _ = _fit_compare_claims(cfg_path, data_dir, tmp_path / "out")

    m1 = next(m for m in fit_payload["models"] if m["model_id"] == "M1")
    assert m1["parameters"]["beta_methyl_to_e2f"]["mean"] < 0.0
    assert m1["parameters"]["beta_mmr_to_mutation"]["mean"] < 0.0

    ranking = compare_payload["ranking"]
    assert len(ranking) == 4
    assert abs(sum(float(r["weight"]) for r in ranking) - 1.0) < 1e-6


def test_known_primary_pass_fixture(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    build_real_dataset(cfg_path, data_dir)

    _, _, claims = _fit_compare_claims(cfg_path, data_dir, tmp_path / "out")

    assert claims["claims"]["1"]["status"] == "pass"
    assert claims["claims"]["2"]["status"] == "pass"
    assert claims["claims"]["3"]["status"] == "pass"
    assert claims["claims"]["4"]["status"] == "pass"


def test_known_alt_wins_fixture_fails_claim5(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    datasets = build_real_dataset(cfg_path, data_dir)
    enforce_alt_mechanism_profile(datasets)
    write_science_datasets(data_dir, datasets)

    _, compare_payload, claims = _fit_compare_claims(cfg_path, data_dir, tmp_path / "out")

    assert claims["claims"]["5"]["status"] == "fail"
    assert compare_payload["top_model"] != "M1" or compare_payload["ranking"][0]["delta"] == 0.0


def test_known_claim1_fail_fixture(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    datasets = build_real_dataset(cfg_path, data_dir)
    flatten_methylation_age_slope(datasets)
    write_science_datasets(data_dir, datasets)

    _, _, claims = _fit_compare_claims(cfg_path, data_dir, tmp_path / "out")
    assert claims["claims"]["1"]["status"] == "fail"


def test_known_claim3_fail_fixture(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    datasets = build_real_dataset(cfg_path, data_dir)
    remove_demethylation_uplift(datasets)
    write_science_datasets(data_dir, datasets)

    _, _, claims = _fit_compare_claims(cfg_path, data_dir, tmp_path / "out")
    assert claims["claims"]["3"]["status"] == "fail"
