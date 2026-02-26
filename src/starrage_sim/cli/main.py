from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from starrage_sim.config import SimulationConfig, load_config
from starrage_sim.data.loaders import SCIENCE_PREPROCESSING
from starrage_sim.data.schemas import SCIENCE_SCHEMA_VERSION
from starrage_sim.engine.science_pipeline import (
    DataValidationError,
    load_json_payload,
    run_claims,
    run_compare,
    run_science_fit,
    run_science_pipeline,
)
from starrage_sim.engine.simulator import run_simulation, write_run_payload
from starrage_sim.engine.sweep import run_sweep
from starrage_sim.metrics.gate_eval import evaluate_gate
from starrage_sim.provenance import build_science_provenance, canonicalize_json_payload, sha256_json, with_science_provenance
from starrage_sim.reports import render_plot_bundle, render_science_bundle, render_verdict_overview


def _cmd_sweep(args: argparse.Namespace) -> int:
    payload = run_sweep(config_path=args.config, profile=args.profile, out_dir=args.out)
    print(json.dumps({"global_tranche1_verdict": payload["gate"]["global_tranche1_verdict"]}, indent=2))
    return 0


def _run_single(config_path: Path, out_dir: Path, profile_override: str | None) -> dict[str, Any]:
    config = load_config(config_path)
    result = run_simulation(config, profile_override=profile_override)
    metrics = write_run_payload(result, config, out_dir)
    gate = evaluate_gate(metrics)
    (out_dir / "tranche1_gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    render_plot_bundle(metrics, out_dir)
    return {"metrics": metrics, "gate": gate}


def _disabled_science_payload(config: SimulationConfig, out_dir: Path) -> dict[str, Any]:
    provenance = build_science_provenance(
        config=config,
        science_data_hash="disabled",
        schema_version=SCIENCE_SCHEMA_VERSION,
        preprocessing_version=str(SCIENCE_PREPROCESSING.get("version", "unknown")),
        preprocessing_hash=sha256_json(SCIENCE_PREPROCESSING),
    )

    claims = {
        str(i): {
            "status": "insufficient_data",
            "details": {"reason": "science_mode.enabled=false"},
        }
        for i in range(1, 6)
    }
    payload = {
        "claims": claims,
        "science_verdict": {
            "pass": False,
            "mode": config.science_mode.data_mode,
            "enabled": False,
            "has_real_data": False,
        },
    }
    payload = with_science_provenance(payload, provenance)
    payload = canonicalize_json_payload(payload)
    (out_dir / "science_claims_verdict.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    model_compare_payload = with_science_provenance(
        {"metric": config.inference.comparison_metric, "ranking": [], "top_model": None},
        provenance,
    )
    model_compare_payload = canonicalize_json_payload(model_compare_payload)
    (out_dir / "model_comparison.json").write_text(
        json.dumps(model_compare_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    validation_payload = with_science_provenance(
        {"mode": config.science_mode.data_mode, "overall_status": "disabled", "datasets": []},
        provenance,
    )
    validation_payload = canonicalize_json_payload(validation_payload)
    (out_dir / "data_validation_report.json").write_text(
        json.dumps(validation_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    diagnostics_payload = with_science_provenance(
        {
            "backend_requested": config.inference.backend,
            "backend_used": "disabled",
            "runtime_profile": config.runtime_profile,
            "per_model": [],
            "summary": {},
        },
        provenance,
    )
    diagnostics_payload = canonicalize_json_payload(diagnostics_payload)
    (out_dir / "science_trace_diagnostics.json").write_text(
        json.dumps(diagnostics_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out_dir / "posterior_summary.csv").write_text("", encoding="utf-8")
    return payload


def _cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(config_path)
    main_payload = _run_single(config_path, out_dir, profile_override=None)

    science_payload: dict[str, Any]
    if config.science_mode.enabled:
        try:
            science = run_science_pipeline(
                config=config,
                out_dir=out_dir,
                data_dir=None,
                runtime_profile=config.runtime_profile,
            )
            science_payload = science["claims"]
            render_science_bundle(
                science_payload,
                science.get("comparison", {}),
                out_dir,
                verdict_policy=dict(config.verdict_policy.__dict__),
            )
        except DataValidationError:
            raise
    else:
        science_payload = _disabled_science_payload(config, out_dir)

    sensitivity_results: list[dict[str, Any]] = []
    for sens_path in config.sensitivity_configs:
        if not sens_path.exists():
            continue
        sens_out = out_dir / "sensitivity" / sens_path.stem
        sens_out.mkdir(parents=True, exist_ok=True)
        payload = _run_single(sens_path, sens_out, profile_override=config.runtime_profile)
        sensitivity_results.append(
            {
                "config": str(sens_path),
                "out_dir": str(sens_out),
                "global_tranche1_verdict": payload["gate"]["global_tranche1_verdict"],
                "milestones": {
                    k: v["pass"] for k, v in payload["gate"]["milestones"].items()
                },
            }
        )

    tranche_pass = bool(main_payload["gate"]["global_tranche1_verdict"])
    science_pass = bool(science_payload.get("science_verdict", {}).get("pass", False))
    combined_pass = tranche_pass and science_pass if config.verdict_policy.science_as_hard_blocker else tranche_pass

    summary = {
        "primary": {
            "config": str(config_path),
            "out_dir": str(out_dir),
            "global_tranche1_verdict": tranche_pass,
            "science_verdict": science_payload.get("science_verdict", {}),
            "milestones": {k: v["pass"] for k, v in main_payload["gate"]["milestones"].items()},
            "combined_decision": {
                "pass": combined_pass,
                "policy": {
                    "science_as_hard_blocker": bool(config.verdict_policy.science_as_hard_blocker)
                },
            },
        },
        "sensitivity": sensitivity_results,
    }

    render_verdict_overview(
        tranche_gate=main_payload["gate"],
        science_payload=science_payload,
        verdict_policy=dict(config.verdict_policy.__dict__),
        out_dir=out_dir,
    )
    (out_dir / "sensitivity_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    return 0


def _cmd_fit(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser().resolve())
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    fit_payload = run_science_fit(
        config=config,
        out_dir=out_dir,
        data_dir=Path(args.data).expanduser().resolve(),
        runtime_profile=config.runtime_profile,
    )

    summary = {
        "fit": str(out_dir / "science_fit.json"),
        "backend_used": fit_payload.get("backend_used"),
        "models": [m.get("model_id") for m in fit_payload.get("models", [])],
        "top_model": fit_payload.get("ranking", [{}])[0].get("model_id") if fit_payload.get("ranking") else None,
    }
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    fit_payload = load_json_payload(args.fit)
    out_path = Path(args.out).expanduser().resolve()
    comparison = run_compare(fit_payload, out_path=out_path)
    print(json.dumps(comparison, indent=2))
    return 0


def _cmd_claims(args: argparse.Namespace) -> int:
    fit_payload = load_json_payload(args.fit)
    compare_payload = load_json_payload(args.compare)

    cfg_source = fit_payload.get("config_source")
    if not cfg_source:
        raise ValueError("fit payload is missing config_source; cannot evaluate claims thresholds")

    config = load_config(cfg_source)
    out_path = Path(args.out).expanduser().resolve()
    verdict = run_claims(
        fit_payload=fit_payload,
        comparison=compare_payload,
        config=config,
        out_path=out_path,
    )
    print(json.dumps(verdict, indent=2))
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    metrics_path = Path(args.metrics).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    gate = evaluate_gate(metrics)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gate, indent=2), encoding="utf-8")
    print(json.dumps(gate, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="starrage-sim",
        description="Tranche + science evidence simulator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run tranche simulation and science pipeline")
    run_p.add_argument("--config", required=True, help="Path to YAML config")
    run_p.add_argument("--out", required=True, help="Output directory")
    run_p.set_defaults(func=_cmd_run)

    fit_p = sub.add_parser("fit", help="Fit competing causal models from science datasets")
    fit_p.add_argument("--config", required=True, help="Path to YAML config")
    fit_p.add_argument("--data", required=True, help="Directory with science CSV files")
    fit_p.add_argument("--out", required=True, help="Output directory")
    fit_p.set_defaults(func=_cmd_fit)

    compare_p = sub.add_parser("compare", help="Compare fitted models with LOO/WAIC")
    compare_p.add_argument("--fit", required=True, help="Path to science_fit.json")
    compare_p.add_argument("--out", required=True, help="Path to output model_comparison.json")
    compare_p.set_defaults(func=_cmd_compare)

    claims_p = sub.add_parser("claims", help="Evaluate claims 1-5 from fit + comparison outputs")
    claims_p.add_argument("--fit", required=True, help="Path to science_fit.json")
    claims_p.add_argument("--compare", required=True, help="Path to model_comparison.json")
    claims_p.add_argument("--out", required=True, help="Path to output science_claims_verdict.json")
    claims_p.set_defaults(func=_cmd_claims)

    sweep_p = sub.add_parser("sweep", help="Run single-profile simulation sweep")
    sweep_p.add_argument("--config", required=True, help="Path to YAML config")
    sweep_p.add_argument("--profile", required=True, choices=["quick", "full"], help="Runtime profile")
    sweep_p.add_argument("--out", required=True, help="Output directory")
    sweep_p.set_defaults(func=_cmd_sweep)

    gate_p = sub.add_parser("gate", help="Evaluate tranche gate from existing metrics JSON")
    gate_p.add_argument("--metrics", required=True, help="Path to metrics_summary.json")
    gate_p.add_argument("--out", required=True, help="Path to output tranche1_gate.json")
    gate_p.set_defaults(func=_cmd_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
