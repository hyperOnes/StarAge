from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from starrage_sim.config import SimulationConfig
from starrage_sim.data.loaders import load_science_data
from starrage_sim.data.validators import DataValidationError
from starrage_sim.inference.diagnostics import posterior_summary_rows, summarize_trace_diagnostics
from starrage_sim.inference.pymc_runner import fit_models
from starrage_sim.metrics.claims_eval import evaluate_claims
from starrage_sim.metrics.model_compare import compare_models
from starrage_sim.provenance import (
    build_science_provenance,
    canonicalize_json_payload,
    extract_science_provenance,
    with_science_provenance,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = canonicalize_json_payload(payload)
    path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_science_fit(
    config: SimulationConfig,
    out_dir: Path,
    data_dir: str | Path | None = None,
    runtime_profile: str = "quick",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = load_science_data(config=config, data_dir_override=data_dir)
    provenance = build_science_provenance(
        config=config,
        science_data_hash=bundle.science_data_hash,
        schema_version=bundle.schema_version,
        preprocessing_version=str(bundle.preprocessing.get("version", "unknown")),
        preprocessing_hash=bundle.preprocessing_hash,
    )
    fit_payload = fit_models(config=config, bundle=bundle, runtime_profile=runtime_profile)
    fit_payload["assumptions_hash"] = provenance["assumptions_hash"]
    fit_payload["config_source"] = str(config.source_path)
    fit_payload["science_data_hash"] = provenance["science_data_hash"]
    fit_payload["git_commit"] = provenance["git_commit"]
    fit_payload["config_hash"] = provenance["config_hash"]
    fit_payload["seed"] = provenance["seed"]
    fit_payload["schema_version"] = provenance["schema_version"]
    fit_payload["preprocessing_version"] = provenance["preprocessing_version"]
    fit_payload["preprocessing_hash"] = provenance["preprocessing_hash"]
    fit_payload["raw_input_hashes"] = dict(bundle.raw_input_hashes)
    fit_payload["canonical_content_hashes"] = dict(bundle.canonical_content_hashes)
    fit_payload["preprocessing"] = dict(bundle.preprocessing)
    fit_payload["claim_thresholds"] = dict(config.claim_thresholds.__dict__)
    fit_payload["locus_panel"] = [loc.__dict__ for loc in config.locus_panel]
    fit_payload["verdict_policy"] = dict(config.verdict_policy.__dict__)
    fit_payload = with_science_provenance(fit_payload, provenance)

    validation_payload = with_science_provenance(dict(bundle.validation_report), provenance)
    diagnostics = summarize_trace_diagnostics(fit_payload)
    diagnostics = with_science_provenance(diagnostics, provenance)

    _write_json(out_dir / "science_fit.json", fit_payload)
    _write_json(out_dir / "data_validation_report.json", validation_payload)

    post_rows = posterior_summary_rows(fit_payload)
    _write_csv(out_dir / "posterior_summary.csv", post_rows)
    _write_json(out_dir / "science_trace_diagnostics.json", diagnostics)

    return fit_payload


def run_compare(fit_payload: dict[str, Any], out_path: Path) -> dict[str, Any]:
    comparison = compare_models(fit_payload, metric=str(fit_payload.get("comparison_metric", "loo")))
    provenance = extract_science_provenance(fit_payload)
    if any(v is not None for v in provenance.values()):
        comparison = with_science_provenance(comparison, provenance)
    _write_json(out_path, comparison)
    return comparison


def run_claims(
    fit_payload: dict[str, Any],
    comparison: dict[str, Any],
    config: SimulationConfig,
    out_path: Path,
) -> dict[str, Any]:
    claims = evaluate_claims(fit_payload=fit_payload, compare_payload=comparison, config=config)
    provenance = extract_science_provenance(fit_payload)
    if any(v is not None for v in provenance.values()):
        claims = with_science_provenance(claims, provenance)
    _write_json(out_path, claims)
    return claims


def run_science_pipeline(
    config: SimulationConfig,
    out_dir: Path,
    data_dir: str | Path | None = None,
    runtime_profile: str = "quick",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    fit_payload = run_science_fit(
        config=config,
        out_dir=out_dir,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
    )
    comparison = run_compare(fit_payload, out_path=out_dir / "model_comparison.json")
    claims = run_claims(
        fit_payload=fit_payload,
        comparison=comparison,
        config=config,
        out_path=out_dir / "science_claims_verdict.json",
    )
    return {
        "fit": fit_payload,
        "comparison": comparison,
        "claims": claims,
    }


def load_json_payload(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    return json.loads(p.read_text(encoding="utf-8"))


__all__ = [
    "DataValidationError",
    "load_json_payload",
    "run_claims",
    "run_compare",
    "run_science_fit",
    "run_science_pipeline",
]
