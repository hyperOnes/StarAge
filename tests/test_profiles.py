from __future__ import annotations

from starrage_sim.config import load_config


def test_profile_budget_relationship() -> None:
    cfg = load_config("configs/default_optimistic.yaml")
    q = cfg.resolved_profile("quick")
    f = cfg.resolved_profile("full")

    assert q.replicates_per_condition < f.replicates_per_condition
    assert q.bootstrap_n < f.bootstrap_n
    assert q.fucci_cells < f.fucci_cells
