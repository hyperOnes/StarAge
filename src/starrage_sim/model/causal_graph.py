from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


STATE_VARIABLES: tuple[str, ...] = (
    "enhancer_methyl_beta",
    "enhancer_open_state",
    "e2f_activity",
    "mmr_sphase_expr",
    "mu_division",
)


@dataclass
class CausalChannels:
    methylation_rows: list[dict[str, Any]]
    e2f_rows: list[dict[str, Any]]
    mmr_rows: list[dict[str, Any]]
    mutation_rows: list[dict[str, Any]]
    viability_rows: list[dict[str, Any]]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        perturb_rows = self.metadata.get("perturbation_rows", [])
        return {
            "metadata": dict(self.metadata),
            "counts": {
                "methylation": len(self.methylation_rows),
                "e2f": len(self.e2f_rows),
                "mmr": len(self.mmr_rows),
                "mutation": len(self.mutation_rows),
                "viability": len(self.viability_rows),
                "perturbation": len(perturb_rows) if isinstance(perturb_rows, list) else 0,
            },
        }


def enhancer_open_state(enhancer_methyl_beta: float) -> float:
    return float(np.clip(1.0 - float(enhancer_methyl_beta), 0.0, 1.0))


def _donor_key(row: dict[str, Any], condition_key: str = "condition") -> tuple[str, str, str]:
    return (str(row["donor_id"]), str(row["tissue"]), str(row[condition_key]))


def build_causal_channels(datasets: dict[str, list[dict[str, Any]]]) -> CausalChannels:
    methyl_rows = [dict(row) for row in datasets.get("methylation_observations", [])]
    e2f_rows = [dict(row) for row in datasets.get("e2f_activity_observations", [])]
    mmr_rows = [dict(row) for row in datasets.get("mmr_expression_observations", [])]
    mut_rows = [dict(row) for row in datasets.get("mutation_observations", [])]
    viability_rows = [dict(row) for row in datasets.get("viability_observations", [])]

    methyl_by_exact: dict[tuple[str, str, str, str, str, str], float] = {}
    for row in methyl_rows:
        key = (
            str(row["donor_id"]),
            str(row["tissue"]),
            str(row["gene"]),
            str(row["locus_id"]),
            str(row["condition"]),
            str(row["age"]),
        )
        beta = float(row["methyl_beta"])
        row["enhancer_methyl_beta"] = beta
        row["enhancer_open_state"] = enhancer_open_state(beta)
        methyl_by_exact[key] = beta

    e2f_by_donor_condition: dict[tuple[str, str, str], list[float]] = {}
    for row in e2f_rows:
        m_key = (
            str(row["donor_id"]),
            str(row["tissue"]),
            str(row["gene"]),
            str(row["locus_id"]),
            str(row["condition"]),
            str(row["age"]),
        )
        methyl_beta = methyl_by_exact.get(m_key)
        if methyl_beta is None:
            methyl_beta = float("nan")

        row["enhancer_methyl_beta"] = methyl_beta
        row["enhancer_open_state"] = enhancer_open_state(methyl_beta) if np.isfinite(methyl_beta) else float("nan")
        row["e2f_activity"] = float(row["e2f_signal"])

        key = _donor_key(row)
        e2f_by_donor_condition.setdefault(key, []).append(float(row["e2f_signal"]))

    donor_condition_e2f_mean: dict[tuple[str, str, str], float] = {
        key: float(np.mean(np.asarray(values, dtype=float)))
        for key, values in e2f_by_donor_condition.items()
        if values
    }

    mmr_sphase_by_donor_condition: dict[tuple[str, str, str], list[float]] = {}
    for row in mmr_rows:
        key = _donor_key(row)
        row["is_s_phase"] = 1 if str(row["phase"]).upper().startswith("S") else 0
        row["e2f_activity"] = donor_condition_e2f_mean.get(key, float("nan"))
        if row["is_s_phase"]:
            mmr_sphase_by_donor_condition.setdefault(key, []).append(float(row["expr_norm"]))

    mmr_sphase_mean: dict[tuple[str, str, str], float] = {
        key: float(np.mean(np.asarray(values, dtype=float)))
        for key, values in mmr_sphase_by_donor_condition.items()
        if values
    }

    viability_map: dict[tuple[str, str, str], float] = {
        _donor_key(row): float(row["viability"]) for row in viability_rows
    }

    for row in mut_rows:
        key = _donor_key(row)
        row["mu_division"] = float(row["mutations_per_division"])
        row["mmr_sphase_expr"] = mmr_sphase_mean.get(key, float("nan"))
        row["e2f_activity"] = donor_condition_e2f_mean.get(key, float("nan"))
        row["viability"] = viability_map.get(key, float("nan"))

    metadata = {
        "state_variables": list(STATE_VARIABLES),
        "has_methylation": len(methyl_rows) > 0,
        "has_e2f": len(e2f_rows) > 0,
        "has_mmr": len(mmr_rows) > 0,
        "has_mutation": len(mut_rows) > 0,
        "has_viability": len(viability_rows) > 0,
    }

    return CausalChannels(
        methylation_rows=methyl_rows,
        e2f_rows=e2f_rows,
        mmr_rows=mmr_rows,
        mutation_rows=mut_rows,
        viability_rows=viability_rows,
        metadata=metadata,
    )
