from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any

from starrage_sim.config import SimulationConfig
from starrage_sim.data.schemas import (
    DATASET_ORDER,
    DatasetSchema,
    SCIENCE_DATASET_SCHEMAS,
    SCIENCE_SCHEMA_VERSION,
    dataset_path,
)
from starrage_sim.data.synthetic_generator import generate_synthetic_science_data
from starrage_sim.data.validators import (
    DataValidationError,
    DatasetValidationResult,
    build_validation_report,
    validate_csv_file,
    validate_rows,
)
from starrage_sim.provenance import sha256_json


@dataclass
class ScienceDataBundle:
    mode: str
    data_mode: str
    datasets: dict[str, list[dict[str, Any]]]
    dataset_modes: dict[str, str]
    dataset_paths: dict[str, str | None]
    validation_report: dict[str, Any]
    schema_version: str
    preprocessing: dict[str, Any]
    preprocessing_hash: str
    raw_input_hashes: dict[str, str]
    canonical_content_hashes: dict[str, str]
    science_data_hash: str

    @property
    def has_real_data(self) -> bool:
        return any(mode == "real" for mode in self.dataset_modes.values())

    @property
    def is_synthetic_only(self) -> bool:
        return all(mode == "synthetic" for mode in self.dataset_modes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "data_mode": self.data_mode,
            "dataset_modes": dict(self.dataset_modes),
            "dataset_paths": dict(self.dataset_paths),
            "validation_report": self.validation_report,
            "schema_version": self.schema_version,
            "preprocessing": dict(self.preprocessing),
            "preprocessing_hash": self.preprocessing_hash,
            "raw_input_hashes": dict(self.raw_input_hashes),
            "canonical_content_hashes": dict(self.canonical_content_hashes),
            "science_data_hash": self.science_data_hash,
        }


SCIENCE_PREPROCESSING = {
    "version": "science-preprocess-v1",
    "steps": [
        "schema-validation-required-columns",
        "type-coercion-numeric-fields",
        "missing-dataset-policy",
        "synthetic-fallback-schema-conformant",
    ],
}
SCIENCE_HASH_FLOAT_DECIMALS = 12
SCIENCE_NA_TOKENS = {"", "na", "n/a", "nan", "null", "none"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows_hash(rows: list[dict[str, Any]]) -> str:
    return sha256_json(rows)


def _is_na_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    if isinstance(value, str) and value.strip().lower() in SCIENCE_NA_TOKENS:
        return True
    return False


def _canonicalize_value(value: Any) -> str:
    if _is_na_value(value):
        return "__NA__"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        rounded = float(round(value, SCIENCE_HASH_FLOAT_DECIMALS))
        if rounded == 0.0:
            rounded = 0.0
        text = f"{rounded:.{SCIENCE_HASH_FLOAT_DECIMALS}f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _schema_signature(schema: DatasetSchema) -> dict[str, Any]:
    numeric = set(schema.numeric_columns)
    return {
        "dataset": schema.key,
        "columns": [
            {
                "name": col,
                "dtype": "float64" if col in numeric else "string",
                "unit": None,
                "scale": None,
            }
            for col in schema.required_columns
        ],
    }


def _canonical_dataset_content_hash(rows: list[dict[str, Any]], schema: DatasetSchema) -> str:
    normalized: list[dict[str, str]] = []
    for row in rows:
        normalized.append(
            {
                col: _canonicalize_value(row.get(col))
                for col in schema.required_columns
            }
        )

    sort_cols = schema.required_columns
    normalized.sort(key=lambda item: tuple(item.get(col, "__NA__") for col in sort_cols))
    return sha256_json(normalized)


def _canonical_locus_panel(config: SimulationConfig) -> list[dict[str, Any]]:
    rows = [
        {
            "tissue": loc.tissue,
            "gene": loc.gene,
            "locus_id": loc.locus_id,
            "chrom": loc.chrom,
            "start": loc.start,
            "end": loc.end,
        }
        for loc in config.locus_panel
    ]
    rows.sort(
        key=lambda item: (
            str(item.get("tissue")),
            str(item.get("gene")),
            str(item.get("locus_id")),
            str(item.get("chrom")),
            int(item["start"]) if item.get("start") is not None else -1,
            int(item["end"]) if item.get("end") is not None else -1,
        )
    )
    return rows


def _compute_science_data_hash(
    config: SimulationConfig,
    dataset_rows: dict[str, list[dict[str, Any]]],
    dataset_modes: dict[str, str],
    canonical_content_hashes: dict[str, str],
    preprocessing_hash: str,
) -> str:
    payload = {
        "schema_version": SCIENCE_SCHEMA_VERSION,
        "schema": {
            key: _schema_signature(SCIENCE_DATASET_SCHEMAS[key])
            for key in DATASET_ORDER
        },
        "locus_panel": _canonical_locus_panel(config),
        "preprocessing": SCIENCE_PREPROCESSING,
        "preprocessing_hash": preprocessing_hash,
        "datasets": {
            key: {
                "mode": dataset_modes.get(key),
                "content_hash": canonical_content_hashes.get(key),
                "row_count": len(dataset_rows.get(key, [])),
            }
            for key in DATASET_ORDER
        },
    }
    return sha256_json(payload)


def resolve_data_dir(config: SimulationConfig, data_dir_override: str | Path | None = None) -> Path:
    if data_dir_override is not None:
        return Path(data_dir_override).expanduser().resolve()
    if config.science_data.data_dir is not None:
        return config.science_data.data_dir
    return (config.source_path.parent / "science_data").resolve()


def load_science_data(
    config: SimulationConfig,
    data_dir_override: str | Path | None = None,
) -> ScienceDataBundle:
    data_mode = config.science_mode.data_mode
    data_dir = resolve_data_dir(config, data_dir_override)

    dataset_rows: dict[str, list[dict[str, Any]]] = {}
    dataset_modes: dict[str, str] = {}
    dataset_paths: dict[str, str | None] = {}
    raw_input_hashes: dict[str, str] = {}
    canonical_content_hashes: dict[str, str] = {}
    preprocessing_hash = sha256_json(SCIENCE_PREPROCESSING)
    report_items: list[DatasetValidationResult] = []

    if data_mode == "synthetic_only":
        synthetic = generate_synthetic_science_data(config)
        for key in DATASET_ORDER:
            schema = SCIENCE_DATASET_SCHEMAS[key]
            rows = validate_rows(synthetic[key], schema)
            dataset_rows[key] = rows
            dataset_modes[key] = "synthetic"
            dataset_paths[key] = None
            raw_input_hashes[key] = _rows_hash(rows)
            canonical_content_hashes[key] = _canonical_dataset_content_hash(rows, schema)
            report_items.append(
                DatasetValidationResult(
                    dataset=key,
                    path=None,
                    source="synthetic",
                    status="valid",
                    row_count=len(rows),
                    errors=[],
                    warnings=[],
                )
            )

        report = build_validation_report(mode=data_mode, items=report_items)
        science_data_hash = _compute_science_data_hash(
            config=config,
            dataset_rows=dataset_rows,
            dataset_modes=dataset_modes,
            canonical_content_hashes=canonical_content_hashes,
            preprocessing_hash=preprocessing_hash,
        )
        return ScienceDataBundle(
            mode="synthetic",
            data_mode=data_mode,
            datasets=dataset_rows,
            dataset_modes=dataset_modes,
            dataset_paths=dataset_paths,
            validation_report=report.to_dict(),
            schema_version=SCIENCE_SCHEMA_VERSION,
            preprocessing=dict(SCIENCE_PREPROCESSING),
            preprocessing_hash=preprocessing_hash,
            raw_input_hashes=raw_input_hashes,
            canonical_content_hashes=canonical_content_hashes,
            science_data_hash=science_data_hash,
        )

    synthetic_cache: dict[str, list[dict[str, Any]]] | None = None
    fatal_errors: list[str] = []

    for key in DATASET_ORDER:
        schema = SCIENCE_DATASET_SCHEMAS[key]
        resolved_path = dataset_path(key, data_dir=data_dir, explicit_paths=config.science_data.paths)
        dataset_paths[key] = str(resolved_path) if resolved_path is not None else None

        if resolved_path is not None and resolved_path.exists():
            try:
                rows, warnings = validate_csv_file(resolved_path, schema)
                dataset_rows[key] = rows
                dataset_modes[key] = "real"
                raw_input_hashes[key] = _sha256_bytes(resolved_path.read_bytes())
                canonical_content_hashes[key] = _canonical_dataset_content_hash(rows, schema)
                report_items.append(
                    DatasetValidationResult(
                        dataset=key,
                        path=str(resolved_path),
                        source="real",
                        status="valid",
                        row_count=len(rows),
                        errors=[],
                        warnings=warnings,
                    )
                )
            except DataValidationError as exc:
                message = str(exc)
                fatal_errors.append(message)
                report_items.append(
                    DatasetValidationResult(
                        dataset=key,
                        path=str(resolved_path),
                        source="real",
                        status="invalid",
                        row_count=0,
                        errors=[message],
                        warnings=[],
                    )
                )
            continue

        # Missing file paths are allowed only in schema_first with synthetic fallback.
        missing_message = (
            f"dataset '{key}' is missing; expected file '{resolved_path}'"
            if resolved_path is not None
            else f"dataset '{key}' is missing and no path/data_dir was configured"
        )
        report_items.append(
            DatasetValidationResult(
                dataset=key,
                path=str(resolved_path) if resolved_path is not None else None,
                source="none",
                status="missing",
                row_count=0,
                errors=[missing_message],
                warnings=[],
            )
        )

        if data_mode == "data_required":
            fatal_errors.append(missing_message)
            continue

        if not config.science_data.allow_synthetic_fallback:
            fatal_errors.append(
                missing_message + " and science_data.allow_synthetic_fallback=false"
            )
            continue

        if synthetic_cache is None:
            synthetic_cache = generate_synthetic_science_data(config)

        synthetic_rows = validate_rows(synthetic_cache[key], schema)
        dataset_rows[key] = synthetic_rows
        dataset_modes[key] = "synthetic"
        raw_input_hashes[key] = _rows_hash(synthetic_rows)
        canonical_content_hashes[key] = _canonical_dataset_content_hash(synthetic_rows, schema)

        # Replace latest report item with synthetic resolution details.
        report_items[-1] = DatasetValidationResult(
            dataset=key,
            path=str(resolved_path) if resolved_path is not None else None,
            source="synthetic",
            status="valid",
            row_count=len(synthetic_rows),
            errors=[],
            warnings=["generated_schema_conformant_synthetic_data"],
        )

    report = build_validation_report(mode=data_mode, items=report_items)
    if fatal_errors:
        combined = " | ".join(fatal_errors[:6])
        raise DataValidationError(combined)

    mode = "real"
    if dataset_modes and all(v == "synthetic" for v in dataset_modes.values()):
        mode = "synthetic"
    elif any(v == "synthetic" for v in dataset_modes.values()):
        mode = "mixed"

    science_data_hash = _compute_science_data_hash(
        config=config,
        dataset_rows=dataset_rows,
        dataset_modes=dataset_modes,
        canonical_content_hashes=canonical_content_hashes,
        preprocessing_hash=preprocessing_hash,
    )

    return ScienceDataBundle(
        mode=mode,
        data_mode=data_mode,
        datasets=dataset_rows,
        dataset_modes=dataset_modes,
        dataset_paths=dataset_paths,
        validation_report=report.to_dict(),
        schema_version=SCIENCE_SCHEMA_VERSION,
        preprocessing=dict(SCIENCE_PREPROCESSING),
        preprocessing_hash=preprocessing_hash,
        raw_input_hashes=raw_input_hashes,
        canonical_content_hashes=canonical_content_hashes,
        science_data_hash=science_data_hash,
    )
