from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
from pathlib import Path
from typing import Any

from starrage_sim.data.schemas import DatasetSchema


class DataValidationError(ValueError):
    pass


@dataclass
class DatasetValidationResult:
    dataset: str
    path: str | None
    source: str
    status: str
    row_count: int
    errors: list[str]
    warnings: list[str]


@dataclass
class DataValidationReport:
    mode: str
    overall_status: str
    datasets: list[DatasetValidationResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "overall_status": self.overall_status,
            "datasets": [asdict(item) for item in self.datasets],
        }


def _normalize_row(row: dict[str, Any], schema: DatasetSchema, row_idx: int) -> tuple[dict[str, Any], list[str]]:
    normalized: dict[str, Any] = {}
    errors: list[str] = []

    for key in schema.required_columns:
        if key not in row:
            errors.append(f"row={row_idx} missing required column '{key}'")
            continue

        value = row[key]
        if value is None:
            errors.append(f"row={row_idx} column '{key}' is null")
            continue

        if isinstance(value, str):
            value = value.strip()
        if value == "":
            errors.append(f"row={row_idx} column '{key}' is empty")
            continue

        if key in schema.numeric_columns:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                errors.append(f"row={row_idx} column '{key}' must be numeric, got '{value}'")
                continue

            if key in {"age", "divisions", "timepoint", "coverage"}:
                if key == "age" or float(parsed).is_integer():
                    parsed = int(round(parsed))
            normalized[key] = parsed
        else:
            normalized[key] = str(value)

    return normalized, errors


def validate_rows(rows: list[dict[str, Any]], schema: DatasetSchema) -> list[dict[str, Any]]:
    if not rows:
        raise DataValidationError(f"dataset '{schema.key}' has zero rows")

    normalized_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, row in enumerate(rows, start=1):
        normalized, row_errors = _normalize_row(row, schema, idx)
        normalized_rows.append(normalized)
        errors.extend(row_errors)

    if errors:
        sample = "; ".join(errors[:8])
        raise DataValidationError(f"dataset '{schema.key}' failed validation: {sample}")

    return normalized_rows


def load_csv_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not path.exists():
        raise DataValidationError(f"missing file: {path}")

    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader]
            if reader.fieldnames is None:
                raise DataValidationError(f"missing CSV header: {path}")
            if len(set(reader.fieldnames)) != len(reader.fieldnames):
                warnings.append("duplicate_header_columns")
            return rows, warnings
    except UnicodeDecodeError as exc:
        raise DataValidationError(f"file is not valid UTF-8: {path}") from exc


def validate_csv_file(path: Path, schema: DatasetSchema) -> tuple[list[dict[str, Any]], list[str]]:
    rows, warnings = load_csv_rows(path)
    if not rows:
        raise DataValidationError(f"dataset '{schema.key}' has no data rows: {path}")

    file_cols = set(rows[0].keys())
    missing_cols = [col for col in schema.required_columns if col not in file_cols]
    if missing_cols:
        raise DataValidationError(
            f"dataset '{schema.key}' missing required columns {missing_cols} in file {path}"
        )

    extra_cols = sorted(file_cols - set(schema.required_columns))
    if extra_cols:
        warnings.append(f"extra_columns:{','.join(extra_cols)}")

    normalized = validate_rows(rows, schema)
    return normalized, warnings


def build_validation_report(mode: str, items: list[DatasetValidationResult]) -> DataValidationReport:
    has_invalid = any(item.status == "invalid" for item in items)
    has_missing = any(item.status == "missing" for item in items)
    overall = "ok"
    if has_invalid:
        overall = "invalid"
    elif has_missing and mode == "data_required":
        overall = "invalid"
    elif has_missing:
        overall = "partial"

    return DataValidationReport(mode=mode, overall_status=overall, datasets=items)
