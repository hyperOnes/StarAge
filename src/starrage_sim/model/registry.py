from __future__ import annotations

from starrage_sim.config import SimulationConfig, TissueParams, WashoutPrior


def get_tissue_params(config: SimulationConfig, tissue: str) -> TissueParams:
    return config.tissue_params[tissue]


def get_washout_prior(config: SimulationConfig, tissue: str) -> WashoutPrior:
    return config.washout_priors[tissue]


def get_baseline_mutation_rate(config: SimulationConfig, tissue: str, donor_age: int) -> float:
    return config.baseline_mutation_rates[tissue][donor_age]
