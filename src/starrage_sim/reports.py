from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_MPL = True
except ModuleNotFoundError:  # pragma: no cover - env dependent
    HAS_MPL = False


def _matrix_from_records(records: list[dict[str, Any]], value_col: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if not records:
        return None

    amps = sorted({float(r["amplification"]) for r in records})
    cyc = sorted({float(r["cyclin_throttle"]) for r in records})
    matrix = np.full((len(cyc), len(amps)), np.nan, dtype=float)

    index_a = {v: i for i, v in enumerate(amps)}
    index_c = {v: i for i, v in enumerate(cyc)}

    for row in records:
        a = float(row["amplification"])
        c = float(row["cyclin_throttle"])
        if value_col in row:
            matrix[index_c[c], index_a[a]] = float(row[value_col])

    return np.asarray(amps), np.asarray(cyc), matrix


def _heatmap(records: list[dict[str, Any]], value_col: str, title: str, out_path: Path) -> None:
    if not HAS_MPL:
        return
    packed = _matrix_from_records(records, value_col)
    if packed is None:
        return

    amps, cyc, matrix = packed

    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    im = ax.imshow(matrix, origin="lower", aspect="auto", cmap="viridis")
    ax.set_title(title)
    ax.set_xlabel("S-Phase amplification")
    ax.set_ylabel("Cyclin throttle")
    ax.set_xticks(np.arange(len(amps)))
    ax.set_xticklabels([f"{v:.2f}" for v in amps], rotation=45, ha="right")
    ax.set_yticks(np.arange(len(cyc)))
    ax.set_yticklabels([f"{v:.2f}" for v in cyc])
    fig.colorbar(im, ax=ax, label=value_col)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _washout_plot(washout_rows: list[dict[str, Any]], out_path: Path) -> None:
    if not HAS_MPL or not washout_rows:
        return

    labels = [f"{row['tissue']}:{row['mark']}" for row in washout_rows]
    means = np.asarray([float(row["half_life_mean"]) for row in washout_rows], dtype=float)
    low = np.asarray([float(row["half_life_ci_low"]) for row in washout_rows], dtype=float)
    high = np.asarray([float(row["half_life_ci_high"]) for row in washout_rows], dtype=float)
    yerr = np.vstack([np.maximum(means - low, 0.0), np.maximum(high - means, 0.0)])

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=150)
    ax.bar(np.arange(len(labels)), means, yerr=yerr, capsize=4)
    ax.set_xticks(np.arange(len(labels)))
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel("Estimated half-life (divisions)")
    ax.set_title("CRISPRon/off washout half-life estimates")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _pareto_plot(metrics: dict[str, Any], out_path: Path) -> None:
    if not HAS_MPL:
        return

    dose_rows = metrics.get("dose_summary", [])
    pareto = metrics.get("pareto", {})
    frontier = pareto.get("frontier_rows", [])
    band = pareto.get("pareto_band_rows", [])
    if not dose_rows:
        return

    def _m(row: dict[str, Any]) -> float:
        return float(row.get("mutation_reduction_hier_mean", row.get("mutation_reduction_mean", np.nan)))

    def _v(row: dict[str, Any]) -> float:
        return float(row.get("viability_hier_mean", row.get("viability_mean", np.nan)))

    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    ax.scatter([_m(r) for r in dose_rows], [_v(r) for r in dose_rows], s=18, alpha=0.35, label="All doses")
    if frontier:
        ax.scatter([_m(r) for r in frontier], [_v(r) for r in frontier], s=28, alpha=0.9, label="Pareto frontier")
    if band:
        ax.scatter([_m(r) for r in band], [_v(r) for r in band], s=34, alpha=0.9, label="Pareto band")
    best = pareto.get("best_feasible_dose")
    if best:
        ax.scatter(
            [float(best.get("mutation_score", np.nan))],
            [float(best.get("viability_score", np.nan))],
            s=80,
            marker="*",
            label="Best feasible",
        )
    ax.set_xlabel("Mutation score")
    ax.set_ylabel("Viability score")
    ax.set_title("Pareto view: mutation vs viability")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def render_plot_bundle(metrics: dict[str, Any], out_dir: Path) -> None:
    plots_dir = out_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    if not HAS_MPL:
        (plots_dir / "plot_warnings.txt").write_text(
            "matplotlib is not installed; PNG plot generation was skipped.\n",
            encoding="utf-8",
        )
        return

    dose_summary = metrics.get("dose_summary", [])
    _heatmap(
        dose_summary,
        value_col="mutation_reduction_ci_low",
        title="Lower 95% CI: Mutation reduction",
        out_path=plots_dir / "mutation_reduction_ci_low.png",
    )
    _heatmap(
        dose_summary,
        value_col="viability_ci_low",
        title="Lower 95% CI: Viability",
        out_path=plots_dir / "viability_ci_low.png",
    )

    objective_table = metrics.get("synergy", {}).get("objective_table", [])
    if objective_table:
        _heatmap(
            objective_table,
            value_col="objective_mean",
            title="Synergy objective surface",
            out_path=plots_dir / "synergy_objective.png",
        )

    _washout_plot(metrics.get("washout_summary", []), plots_dir / "washout_half_life.png")
    _pareto_plot(metrics, plots_dir / "pareto_frontier.png")


def render_science_bundle(
    claims_payload: dict[str, Any],
    comparison_payload: dict[str, Any],
    out_dir: Path,
    verdict_policy: dict[str, Any] | None = None,
) -> None:
    science_dir = out_dir / "science_reports"
    science_dir.mkdir(parents=True, exist_ok=True)

    claims = claims_payload.get("claims", {})
    lines = [
        "# Science Claims Verdict",
        "",
        "| Claim | Status |",
        "|---|---|",
    ]
    for claim_id in ("1", "2", "3", "4", "5"):
        status = claims.get(claim_id, {}).get("status", "insufficient_data")
        lines.append(f"| {claim_id} | {status} |")

    science_verdict = claims_payload.get("science_verdict", {})
    lines.extend(
        [
            "",
            f"- Science pass: `{bool(science_verdict.get('pass', False))}`",
            f"- Data mode: `{science_verdict.get('mode', 'unknown')}`",
            f"- Top model: `{science_verdict.get('top_model', comparison_payload.get('top_model'))}`",
            f"- Verdict policy: `{verdict_policy or {}}`",
        ]
    )
    (science_dir / "claims_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not HAS_MPL:
        return

    ranking = comparison_payload.get("ranking", [])
    if not ranking:
        return

    labels = [str(row.get("model_id")) for row in ranking]
    weights = np.asarray([float(row.get("weight", 0.0)) for row in ranking], dtype=float)
    fig, ax = plt.subplots(figsize=(7, 4), dpi=150)
    ax.bar(np.arange(len(labels)), weights)
    ax.set_xticks(np.arange(len(labels)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Model weight")
    ax.set_title(f"Model comparison ({comparison_payload.get('metric', 'loo')})")
    fig.tight_layout()
    fig.savefig(science_dir / "model_weights.png")
    plt.close(fig)


def render_verdict_overview(
    tranche_gate: dict[str, Any],
    science_payload: dict[str, Any],
    verdict_policy: dict[str, Any],
    out_dir: Path,
) -> None:
    out_path = out_dir / "verdict_overview.md"
    tranche_pass = bool(tranche_gate.get("global_tranche1_verdict", False))
    science_verdict = science_payload.get("science_verdict", {})
    science_pass = bool(science_verdict.get("pass", False))

    lines = [
        "# Two-Verdict Overview",
        "",
        "## Tranche Gate (Contractual Go/No-Go)",
        f"- Result: `{tranche_pass}`",
        "- Source: `tranche1_gate.json`",
        "",
        "## Science Verdict (Mechanism/Claim Adjudication)",
        f"- Result: `{science_pass}`",
        f"- Mode: `{science_verdict.get('mode', 'unknown')}`",
        f"- Top model: `{science_verdict.get('top_model', 'unknown')}`",
        "- Source: `science_claims_verdict.json`",
        "",
        "## Policy",
        f"- `verdict_policy`: `{verdict_policy}`",
        "- Interpretation: tranche and science are intentionally separated unless policy explicitly links them.",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
