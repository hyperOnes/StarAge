from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from starrage_sim.config import ConfigError, load_config
from starrage_sim.data.loaders import load_science_data
from starrage_sim.data.schemas import SCIENCE_DATASET_SCHEMAS
from starrage_sim.data.validators import DataValidationError
from starrage_sim.engine.science_pipeline import run_science_fit
from starrage_sim.metrics.claims_eval import evaluate_claims
from starrage_sim.metrics.model_compare import compare_models
from tests.science_utils import build_real_dataset, write_science_config


def test_schema_validation_rejects_malformed_files(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    datasets = build_real_dataset(cfg_path, data_dir)

    # Corrupt one required file by dropping a mandatory column.
    bad_rows = []
    for row in datasets["methylation_observations"]:
        row = dict(row)
        row.pop("coverage", None)
        bad_rows.append(row)

    methyl_path = data_dir / SCIENCE_DATASET_SCHEMAS["methylation_observations"].filename
    with methyl_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(bad_rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in bad_rows:
            writer.writerow(row)

    config = load_config(cfg_path)
    with pytest.raises(DataValidationError, match="missing required columns"):
        load_science_data(config=config, data_dir_override=data_dir)


def test_schema_first_missing_data_uses_synthetic_fallback(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="schema_first", allow_synthetic_fallback=True)
    config = load_config(cfg_path)

    out_dir = tmp_path / "out"
    payload = run_science_fit(config=config, out_dir=out_dir, data_dir=tmp_path / "missing")

    assert payload["is_synthetic_only"]
    datasets = payload["validation_report"]["datasets"]
    assert datasets
    assert all(d["status"] == "valid" for d in datasets)
    assert all(d["source"] == "synthetic" for d in datasets)


def test_data_required_missing_file_hard_fails(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    config = load_config(cfg_path)

    with pytest.raises(DataValidationError, match="is missing"):
        run_science_fit(config=config, out_dir=tmp_path / "out", data_dir=tmp_path / "empty")


def test_inference_sensitivity_backend_validation(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw.setdefault("inference", {})["sensitivity_backend"] = "invalid_mode"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="sensitivity_backend"):
        load_config(cfg_path)


def test_synthetic_only_unvalidated_cannot_science_pass_by_default(tmp_path: Path) -> None:
    cfg_path = write_science_config(
        tmp_path / "science.json",
        data_mode="synthetic_only",
        allow_synthetic_fallback=True,
    )
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["inference"]["backend"] = "surrogate"
    raw["claim_thresholds"]["posterior_probability"] = 0.5
    raw["claim_thresholds"]["claim5"]["model_margin_min"] = 0.0
    raw["claim_thresholds"]["claim5"]["stability_min"] = 0.0
    raw["verdict_policy"]["allow_synthetic_science_pass_for_dev"] = False
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")

    config = load_config(cfg_path)
    fit = run_science_fit(config=config, out_dir=tmp_path / "out")
    compare_payload = compare_models(fit, metric="loo")
    claims = evaluate_claims(fit_payload=fit, compare_payload=compare_payload, config=config)

    assert any(c["status"] == "synthetic_only_unvalidated" for c in claims["claims"].values())
    assert claims["science_verdict"]["pass"] is False


def test_science_data_hash_is_order_independent_for_locus_panel(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    build_real_dataset(cfg_path, data_dir)

    baseline_config = load_config(cfg_path)
    baseline_bundle = load_science_data(config=baseline_config, data_dir_override=data_dir)

    reordered = json.loads(cfg_path.read_text(encoding="utf-8"))
    reordered["locus_panel"] = list(reversed(reordered["locus_panel"]))
    reordered_cfg_path = tmp_path / "science_reordered_panel.json"
    reordered_cfg_path.write_text(json.dumps(reordered), encoding="utf-8")

    reordered_config = load_config(reordered_cfg_path)
    reordered_bundle = load_science_data(config=reordered_config, data_dir_override=data_dir)

    assert baseline_bundle.science_data_hash == reordered_bundle.science_data_hash


def test_science_data_hash_uses_canonical_content_not_file_row_order(tmp_path: Path) -> None:
    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    data_dir = tmp_path / "data"
    build_real_dataset(cfg_path, data_dir)

    config = load_config(cfg_path)
    before = load_science_data(config=config, data_dir_override=data_dir)

    methyl_schema = SCIENCE_DATASET_SCHEMAS["methylation_observations"]
    methyl_path = data_dir / methyl_schema.filename
    with methyl_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        fieldnames = list(reader.fieldnames or [])

    rows = list(reversed(rows))
    with methyl_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    after = load_science_data(config=config, data_dir_override=data_dir)

    assert before.raw_input_hashes["methylation_observations"] != after.raw_input_hashes["methylation_observations"]
    assert before.science_data_hash == after.science_data_hash
