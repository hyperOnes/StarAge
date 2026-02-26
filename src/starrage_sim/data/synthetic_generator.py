from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from starrage_sim.config import LocusSpec, SimulationConfig
from starrage_sim.data.schemas import DATASET_ORDER


MMR_GENES: tuple[str, ...] = ("MSH2", "MSH6", "MLH1", "PMS2")
PHASES: tuple[str, ...] = ("G1", "S", "G2M")


def _clip(value: float, low: float, high: float) -> float:
    return float(np.clip(value, low, high))


def _loci_by_tissue(locus_panel: list[LocusSpec], tissues: list[str]) -> dict[str, list[LocusSpec]]:
    grouped: dict[str, list[LocusSpec]] = defaultdict(list)
    for locus in locus_panel:
        grouped[locus.tissue].append(locus)

    # Fall back to synthetic loci if a tissue has no explicit entries.
    for tissue in tissues:
        if grouped.get(tissue):
            continue
        for idx, gene in enumerate(MMR_GENES, start=1):
            grouped[tissue].append(LocusSpec(tissue=tissue, gene=gene, locus_id=f"{tissue}_{gene}_L{idx}"))
    return grouped


def generate_synthetic_science_data(
    config: SimulationConfig,
    requested_keys: list[str] | None = None,
    seed_offset: int = 97,
) -> dict[str, list[dict[str, Any]]]:
    keys = requested_keys or list(DATASET_ORDER)
    rng = np.random.default_rng(int(config.seed) + int(seed_offset))

    loci_by_tissue = _loci_by_tissue(config.locus_panel, config.tissues)

    methyl_rows: list[dict[str, Any]] = []
    e2f_rows: list[dict[str, Any]] = []
    mmr_rows: list[dict[str, Any]] = []
    mutation_rows: list[dict[str, Any]] = []
    perturb_rows: list[dict[str, Any]] = []
    viability_rows: list[dict[str, Any]] = []

    donor_s_state: dict[tuple[str, str, str], float] = {}
    donor_e2f_state: dict[tuple[str, str], float] = {}

    for tissue_idx, tissue in enumerate(config.tissues):
        loci = loci_by_tissue[tissue]
        for age in config.donor_ages:
            age_norm = (float(age) - 25.0) / 35.0
            for donor_ix in range(3):
                donor_id = f"{tissue}_A{age}_D{donor_ix + 1}"
                batch = f"B{1 + donor_ix % 2}"
                demeth_target = loci[(donor_ix + tissue_idx) % len(loci)].locus_id

                baseline_e2f_vals: list[float] = []
                demeth_e2f_vals: list[float] = []

                for locus_ix, locus in enumerate(loci):
                    locus_shift = 0.018 * locus_ix
                    meth_base = _clip(0.18 + 0.16 * age_norm + locus_shift + rng.normal(0.0, 0.015), 0.02, 0.95)
                    meth_demeth = _clip(
                        meth_base - (0.16 if locus.locus_id == demeth_target else 0.02) + rng.normal(0.0, 0.01),
                        0.01,
                        0.95,
                    )

                    e2f_base = _clip(1.45 - 1.10 * meth_base + rng.normal(0.0, 0.04), 0.05, 3.0)
                    e2f_demeth = _clip(1.48 - 1.10 * meth_demeth + rng.normal(0.0, 0.04), 0.05, 3.0)

                    baseline_e2f_vals.append(e2f_base)
                    demeth_e2f_vals.append(e2f_demeth)

                    methyl_rows.append(
                        {
                            "donor_id": donor_id,
                            "tissue": tissue,
                            "age": int(age),
                            "gene": locus.gene,
                            "locus_id": locus.locus_id,
                            "methyl_beta": round(meth_base, 6),
                            "coverage": round(_clip(42 + rng.normal(0.0, 8.0), 12, 120), 2),
                            "batch": batch,
                            "condition": "baseline",
                        }
                    )
                    methyl_rows.append(
                        {
                            "donor_id": donor_id,
                            "tissue": tissue,
                            "age": int(age),
                            "gene": locus.gene,
                            "locus_id": locus.locus_id,
                            "methyl_beta": round(meth_demeth, 6),
                            "coverage": round(_clip(44 + rng.normal(0.0, 7.0), 12, 120), 2),
                            "batch": batch,
                            "condition": f"demethylated::{demeth_target}",
                        }
                    )

                    e2f_rows.append(
                        {
                            "donor_id": donor_id,
                            "tissue": tissue,
                            "age": int(age),
                            "gene": locus.gene,
                            "locus_id": locus.locus_id,
                            "e2f_signal": round(e2f_base, 6),
                            "assay_type": "chip_seq",
                            "batch": batch,
                            "condition": "baseline",
                        }
                    )
                    e2f_rows.append(
                        {
                            "donor_id": donor_id,
                            "tissue": tissue,
                            "age": int(age),
                            "gene": locus.gene,
                            "locus_id": locus.locus_id,
                            "e2f_signal": round(e2f_demeth, 6),
                            "assay_type": "chip_seq",
                            "batch": batch,
                            "condition": f"demethylated::{demeth_target}",
                        }
                    )

                baseline_e2f = float(np.mean(np.asarray(baseline_e2f_vals, dtype=float)))
                demeth_e2f = float(np.mean(np.asarray(demeth_e2f_vals, dtype=float)))
                donor_e2f_state[(donor_id, "baseline")] = baseline_e2f
                donor_e2f_state[(donor_id, "demethylated")] = demeth_e2f

                perturb_rows.append(
                    {
                        "donor_id": donor_id,
                        "tissue": tissue,
                        "intervention": "targeted_demethylation",
                        "target_locus": demeth_target,
                        "dose": round(_clip(1.0 + rng.normal(0.0, 0.08), 0.7, 1.3), 4),
                        "timepoint": 24,
                        "condition_id": f"demethylated::{demeth_target}",
                    }
                )

                for gene_idx, gene in enumerate(MMR_GENES):
                    gene_shift = 0.02 * gene_idx
                    for phase in PHASES:
                        phase_bonus = 0.95 if phase == "S" else 0.20

                        expr_base = _clip(
                            0.55 + 0.52 * baseline_e2f + phase_bonus + gene_shift + rng.normal(0.0, 0.06),
                            0.05,
                            6.0,
                        )
                        expr_demeth = _clip(
                            0.55
                            + 0.52 * demeth_e2f
                            + phase_bonus
                            + gene_shift
                            + (0.18 if phase == "S" else 0.04)
                            + rng.normal(0.0, 0.06),
                            0.05,
                            6.0,
                        )

                        mmr_rows.append(
                            {
                                "donor_id": donor_id,
                                "tissue": tissue,
                                "age": int(age),
                                "gene": gene,
                                "phase": phase,
                                "expr_norm": round(expr_base, 6),
                                "assay_type": "rna_seq",
                                "batch": batch,
                                "condition": "baseline",
                            }
                        )
                        mmr_rows.append(
                            {
                                "donor_id": donor_id,
                                "tissue": tissue,
                                "age": int(age),
                                "gene": gene,
                                "phase": phase,
                                "expr_norm": round(expr_demeth, 6),
                                "assay_type": "rna_seq",
                                "batch": batch,
                                "condition": f"demethylated::{demeth_target}",
                            }
                        )

                        if phase == "S":
                            donor_s_state[(donor_id, "baseline", gene)] = expr_base
                            donor_s_state[(donor_id, "demethylated", gene)] = expr_demeth

                s_base = float(np.mean([donor_s_state[(donor_id, "baseline", g)] for g in MMR_GENES]))
                s_demeth = float(np.mean([donor_s_state[(donor_id, "demethylated", g)] for g in MMR_GENES]))

                mut_base = _clip(
                    1.35 + 0.28 * age_norm - 0.16 * s_base + rng.normal(0.0, 0.03),
                    0.01,
                    3.0,
                )
                mut_demeth = _clip(
                    1.35 + 0.28 * age_norm - 0.16 * s_demeth - 0.05 + rng.normal(0.0, 0.03),
                    0.01,
                    3.0,
                )

                mutation_rows.append(
                    {
                        "donor_id": donor_id,
                        "tissue": tissue,
                        "age": int(age),
                        "condition": "baseline",
                        "divisions": 50,
                        "mutations_per_division": round(mut_base, 6),
                        "assay_type": "wgs",
                        "batch": batch,
                    }
                )
                mutation_rows.append(
                    {
                        "donor_id": donor_id,
                        "tissue": tissue,
                        "age": int(age),
                        "condition": f"demethylated::{demeth_target}",
                        "divisions": 50,
                        "mutations_per_division": round(mut_demeth, 6),
                        "assay_type": "wgs",
                        "batch": batch,
                    }
                )

                viability_rows.append(
                    {
                        "donor_id": donor_id,
                        "tissue": tissue,
                        "condition": "baseline",
                        "viability": round(_clip(0.93 + rng.normal(0.0, 0.015), 0.5, 1.0), 6),
                        "timepoint": 24,
                    }
                )
                viability_rows.append(
                    {
                        "donor_id": donor_id,
                        "tissue": tissue,
                        "condition": f"demethylated::{demeth_target}",
                        "viability": round(_clip(0.90 + rng.normal(0.0, 0.02), 0.45, 1.0), 6),
                        "timepoint": 24,
                    }
                )

    payload = {
        "methylation_observations": methyl_rows,
        "e2f_activity_observations": e2f_rows,
        "mmr_expression_observations": mmr_rows,
        "mutation_observations": mutation_rows,
        "perturbation_events": perturb_rows,
        "viability_observations": viability_rows,
    }

    return {k: payload[k] for k in keys}
