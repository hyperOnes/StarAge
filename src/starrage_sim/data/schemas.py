from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatasetSchema:
    key: str
    filename: str
    required_columns: tuple[str, ...]
    numeric_columns: tuple[str, ...]


SCIENCE_SCHEMA_VERSION = "science-schema-v1"


DATASET_ORDER: tuple[str, ...] = (
    "methylation_observations",
    "e2f_activity_observations",
    "mmr_expression_observations",
    "mutation_observations",
    "perturbation_events",
    "viability_observations",
)


SCIENCE_DATASET_SCHEMAS: dict[str, DatasetSchema] = {
    "methylation_observations": DatasetSchema(
        key="methylation_observations",
        filename="methylation_observations.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "age",
            "gene",
            "locus_id",
            "methyl_beta",
            "coverage",
            "batch",
            "condition",
        ),
        numeric_columns=("age", "methyl_beta", "coverage"),
    ),
    "e2f_activity_observations": DatasetSchema(
        key="e2f_activity_observations",
        filename="e2f_activity_observations.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "age",
            "gene",
            "locus_id",
            "e2f_signal",
            "assay_type",
            "batch",
            "condition",
        ),
        numeric_columns=("age", "e2f_signal"),
    ),
    "mmr_expression_observations": DatasetSchema(
        key="mmr_expression_observations",
        filename="mmr_expression_observations.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "age",
            "gene",
            "phase",
            "expr_norm",
            "assay_type",
            "batch",
            "condition",
        ),
        numeric_columns=("age", "expr_norm"),
    ),
    "mutation_observations": DatasetSchema(
        key="mutation_observations",
        filename="mutation_observations.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "age",
            "condition",
            "divisions",
            "mutations_per_division",
            "assay_type",
            "batch",
        ),
        numeric_columns=("age", "divisions", "mutations_per_division"),
    ),
    "perturbation_events": DatasetSchema(
        key="perturbation_events",
        filename="perturbation_events.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "intervention",
            "target_locus",
            "dose",
            "timepoint",
            "condition_id",
        ),
        numeric_columns=("dose", "timepoint"),
    ),
    "viability_observations": DatasetSchema(
        key="viability_observations",
        filename="viability_observations.csv",
        required_columns=(
            "donor_id",
            "tissue",
            "condition",
            "viability",
            "timepoint",
        ),
        numeric_columns=("viability", "timepoint"),
    ),
}


def dataset_path(
    dataset_key: str,
    data_dir: Path | None,
    explicit_paths: dict[str, Path],
) -> Path | None:
    if dataset_key in explicit_paths:
        return explicit_paths[dataset_key]
    if data_dir is None:
        return None
    schema = SCIENCE_DATASET_SCHEMAS[dataset_key]
    return (data_dir / schema.filename).resolve()
