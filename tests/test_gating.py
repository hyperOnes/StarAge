from __future__ import annotations

from starrage_sim.metrics.gating import summarize_fucci


def test_fucci_summary_has_expected_fields() -> None:
    folds = [2.1, 2.3, 1.9, 2.2]
    leak = [0.30, 0.35, 0.33, 0.31]
    summary = summarize_fucci(folds, leak)

    assert "sphase_fold" in summary
    assert "leakage" in summary
    assert summary["sphase_fold"]["mean"] > 2.0
    assert 0.2 < summary["leakage"]["mean"] < 0.5
