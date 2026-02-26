from __future__ import annotations

import numpy as np

from starrage_sim.metrics.statistics import summarize


def summarize_fucci(folds: np.ndarray | list[float], leakages: np.ndarray | list[float]) -> dict[str, dict[str, float]]:
    return {
        "sphase_fold": summarize(np.asarray(folds, dtype=float)),
        "leakage": summarize(np.asarray(leakages, dtype=float)),
    }
