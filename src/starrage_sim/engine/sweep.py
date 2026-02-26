from __future__ import annotations

from pathlib import Path
from typing import Any

from starrage_sim.config import load_config
from starrage_sim.engine.simulator import run_simulation, write_run_payload
from starrage_sim.metrics.gate_eval import evaluate_gate
from starrage_sim.reports import render_plot_bundle


def run_sweep(config_path: str, profile: str, out_dir: str) -> dict[str, Any]:
    config = load_config(config_path)
    result = run_simulation(config, profile_override=profile)
    out = Path(out_dir)
    metrics = write_run_payload(result, config, out)
    gate = evaluate_gate(metrics)
    (out / "tranche1_gate.json").write_text(__import__("json").dumps(gate, indent=2), encoding="utf-8")
    render_plot_bundle(metrics, out)
    return {"metrics": metrics, "gate": gate}
