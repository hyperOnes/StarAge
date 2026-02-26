from __future__ import annotations

import json
from pathlib import Path

from starrage_sim.cli.main import main
from tests.science_utils import write_science_config


def test_run_produces_tranche_and_science_verdicts(tmp_path: Path) -> None:
    cfg_path = write_science_config(
        tmp_path / "science.json",
        data_mode="synthetic_only",
        allow_synthetic_fallback=True,
        science_enabled=True,
    )
    out_dir = tmp_path / "run"

    code = main(["run", "--config", str(cfg_path), "--out", str(out_dir)])
    assert code == 0

    gate = json.loads((out_dir / "tranche1_gate.json").read_text(encoding="utf-8"))
    science = json.loads((out_dir / "science_claims_verdict.json").read_text(encoding="utf-8"))
    fit = json.loads((out_dir / "science_fit.json").read_text(encoding="utf-8"))
    summary = json.loads((out_dir / "sensitivity_summary.json").read_text(encoding="utf-8"))

    assert "global_tranche1_verdict" in gate
    assert "science_verdict" in science
    assert all(str(i) in science["claims"] for i in range(1, 6))
    assert fit["sensitivity_backend_requested"] in {"surrogate", "advi"}
    assert fit["sensitivity_backend_used"] in {"surrogate", "pymc_advi_only"}
    assert "global_tranche1_verdict" in summary["primary"]
    assert "science_verdict" in summary["primary"]
