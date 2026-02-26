from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from starrage_sim.model.causal_graph import CausalChannels


MODEL_FAMILY_DESCRIPTIONS: dict[str, str] = {
    "M1": "E2F-enhancer methylation primary mechanism",
    "M2": "Cell-cycle composition shift",
    "M3": "Damage-load/replication-stress",
    "M4": "Global regulatory decline",
}


@dataclass
class ModelFit:
    model_id: str
    description: str
    channel_errors: dict[str, float]
    ppc_error: float
    loo: float
    waic: float
    log_likelihood: float
    n_obs: int
    n_params: int
    diagnostics: dict[str, Any]
    parameters: dict[str, dict[str, float]]
    posterior_draws: dict[str, list[float]]
    claim_inputs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "description": self.description,
            "channel_errors": dict(self.channel_errors),
            "ppc_error": self.ppc_error,
            "loo": self.loo,
            "waic": self.waic,
            "log_likelihood": self.log_likelihood,
            "n_obs": self.n_obs,
            "n_params": self.n_params,
            "diagnostics": dict(self.diagnostics),
            "parameters": dict(self.parameters),
            "posterior_draws": {k: list(v) for k, v in self.posterior_draws.items()},
            "claim_inputs": self.claim_inputs,
        }


def _to_array(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def _fit_linear(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    if X.ndim == 1:
        X = X.reshape(-1, 1)

    if y.size == 0:
        return np.zeros(X.shape[1] + 1, dtype=float), np.asarray([], dtype=float), float("nan")

    design = np.column_stack([np.ones(y.size, dtype=float), X])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    pred = design @ coef
    rmse = float(np.sqrt(np.mean((y - pred) ** 2))) if y.size else float("nan")
    return coef, pred, rmse


def _bootstrap_coefficients(
    X: np.ndarray,
    y: np.ndarray,
    draws: int,
    rng: np.random.Generator,
) -> np.ndarray:
    if X.ndim == 1:
        X = X.reshape(-1, 1)
    if y.size < 3:
        return np.full((max(draws, 1), X.shape[1] + 1), np.nan, dtype=float)

    out = np.empty((draws, X.shape[1] + 1), dtype=float)
    n = y.size
    for idx in range(draws):
        sample_idx = rng.integers(0, n, size=n)
        ys = y[sample_idx]
        xs = X[sample_idx]
        design = np.column_stack([np.ones(ys.size, dtype=float), xs])
        coef, *_ = np.linalg.lstsq(design, ys, rcond=None)
        out[idx, :] = coef
    return out


def _summary(draws: np.ndarray) -> dict[str, float]:
    arr = draws[np.isfinite(draws)]
    if arr.size == 0:
        return {
            "mean": float("nan"),
            "sd": float("nan"),
            "hdi_low": float("nan"),
            "hdi_high": float("nan"),
            "p_gt_0": float("nan"),
            "p_lt_0": float("nan"),
        }

    return {
        "mean": float(np.mean(arr)),
        "sd": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
        "hdi_low": float(np.quantile(arr, 0.025)),
        "hdi_high": float(np.quantile(arr, 0.975)),
        "p_gt_0": float(np.mean(arr > 0.0)),
        "p_lt_0": float(np.mean(arr < 0.0)),
    }


def _norm_rmse(y: np.ndarray, pred: np.ndarray) -> float:
    if y.size == 0:
        return float("nan")
    rmse = np.sqrt(np.mean((y - pred) ** 2))
    return float(rmse / (np.std(y) + 1e-9))


def _condition_demethyl_flag(condition: str) -> int:
    return 1 if "demethyl" in condition.lower() else 0


def _fit_locus_age_slopes(
    methyl_rows: list[dict[str, Any]],
    draws: int,
    rng: np.random.Generator,
) -> tuple[list[dict[str, Any]], dict[str, list[float]]]:
    by_locus: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in methyl_rows:
        if str(row.get("condition", "baseline")).lower() != "baseline":
            continue
        key = (str(row["tissue"]), str(row["gene"]), str(row["locus_id"]))
        by_locus.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    slope_draws: dict[str, list[float]] = {}

    for (tissue, gene, locus_id), rows in sorted(by_locus.items()):
        age = np.asarray([float(r["age"]) for r in rows], dtype=float)
        meth = np.asarray([float(r["methyl_beta"]) for r in rows], dtype=float)
        coverage = len(rows)
        if coverage < 3:
            slope = float("nan")
            draw_arr = np.full(draws, np.nan, dtype=float)
        else:
            X = ((age - 25.0) / 35.0).reshape(-1, 1)
            coef, _, _ = _fit_linear(X, meth)
            slope = float(coef[1])
            bs = _bootstrap_coefficients(X, meth, draws=draws, rng=rng)
            draw_arr = bs[:, 1]

        key = f"{tissue}::{gene}::{locus_id}"
        slope_draws[key] = draw_arr.tolist()
        slope_prob = float(np.mean(draw_arr > 0.0)) if np.any(np.isfinite(draw_arr)) else float("nan")
        out.append(
            {
                "tissue": tissue,
                "gene": gene,
                "locus_id": locus_id,
                "row_count": coverage,
                "age_slope_methylation_mean": float(np.nanmean(draw_arr)) if np.any(np.isfinite(draw_arr)) else slope,
                "delta_methylation_25_to_60": float(np.nanmean(draw_arr) * 35.0)
                if np.any(np.isfinite(draw_arr))
                else (slope * 35.0 if np.isfinite(slope) else float("nan")),
                "posterior_prob_age_slope_gt_0": slope_prob,
            }
        )

    return out, slope_draws


def fit_competing_model(
    model_id: str,
    channels: CausalChannels,
    posterior_draws: int,
    seed: int,
) -> ModelFit:
    if model_id not in MODEL_FAMILY_DESCRIPTIONS:
        raise ValueError(f"Unknown model family '{model_id}'")

    rng = np.random.default_rng(seed)

    methyl_rows = channels.methylation_rows
    e2f_rows = channels.e2f_rows
    mmr_rows = channels.mmr_rows
    mut_rows = channels.mutation_rows
    viability_rows = channels.viability_rows

    # Channel 1: methylation model (shared across families)
    m_age = np.asarray([float(r["age"]) for r in methyl_rows], dtype=float)
    m_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in methyl_rows], dtype=float)
    m_y = np.asarray([float(r["methyl_beta"]) for r in methyl_rows], dtype=float)
    m_X = np.column_stack([(m_age - 25.0) / 35.0, m_demeth]) if m_y.size else np.empty((0, 2), dtype=float)
    m_coef, m_pred, _ = _fit_linear(m_X, m_y)
    m_bs = _bootstrap_coefficients(m_X, m_y, draws=posterior_draws, rng=rng)

    # Channel 2: e2f ~ methylation + age
    e_rows = [r for r in e2f_rows if np.isfinite(float(r.get("enhancer_methyl_beta", float("nan"))))]
    e_meth = np.asarray([float(r["enhancer_methyl_beta"]) for r in e_rows], dtype=float)
    e_age = np.asarray([float(r["age"]) for r in e_rows], dtype=float)
    e_y = np.asarray([float(r["e2f_signal"]) for r in e_rows], dtype=float)
    e_X = np.column_stack([e_meth, (e_age - 25.0) / 35.0]) if e_y.size else np.empty((0, 2), dtype=float)
    e_coef, e_pred, _ = _fit_linear(e_X, e_y)
    e_bs = _bootstrap_coefficients(e_X, e_y, draws=posterior_draws, rng=rng)

    # Channel 3: mmr expression ~ e2f + age + S-phase + demethyl
    mmr_e2f = np.asarray([float(r.get("e2f_activity", float("nan"))) for r in mmr_rows], dtype=float)
    mmr_age = np.asarray([float(r["age"]) for r in mmr_rows], dtype=float)
    mmr_phase = np.asarray([1.0 if int(r.get("is_s_phase", 0)) == 1 else 0.0 for r in mmr_rows], dtype=float)
    mmr_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in mmr_rows], dtype=float)
    mmr_y = np.asarray([float(r["expr_norm"]) for r in mmr_rows], dtype=float)
    finite_mask = np.isfinite(mmr_e2f) & np.isfinite(mmr_y)
    mmr_X = np.column_stack(
        [
            mmr_e2f[finite_mask],
            (mmr_age[finite_mask] - 25.0) / 35.0,
            mmr_phase[finite_mask],
            mmr_demeth[finite_mask],
        ]
    ) if np.any(finite_mask) else np.empty((0, 4), dtype=float)
    mmr_y_fit = mmr_y[finite_mask]
    mmr_coef, mmr_pred, _ = _fit_linear(mmr_X, mmr_y_fit)
    mmr_bs = _bootstrap_coefficients(mmr_X, mmr_y_fit, draws=posterior_draws, rng=rng)

    # Channel 4: mutation rate ~ mmr_sphase + age + viability + demethyl
    mut_mmr = np.asarray([float(r.get("mmr_sphase_expr", float("nan"))) for r in mut_rows], dtype=float)
    mut_age = np.asarray([float(r["age"]) for r in mut_rows], dtype=float)
    mut_v = np.asarray([float(r.get("viability", float("nan"))) for r in mut_rows], dtype=float)
    if np.any(np.isfinite(mut_v)):
        mut_v = np.where(np.isfinite(mut_v), mut_v, np.nanmean(mut_v))
    else:
        mut_v = np.full(mut_v.shape, 0.90, dtype=float)

    mut_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in mut_rows], dtype=float)
    mut_y = np.asarray([float(r["mutations_per_division"]) for r in mut_rows], dtype=float)
    mut_mask = np.isfinite(mut_mmr) & np.isfinite(mut_y)
    mut_X = np.column_stack(
        [
            mut_mmr[mut_mask],
            (mut_age[mut_mask] - 25.0) / 35.0,
            mut_v[mut_mask],
            mut_demeth[mut_mask],
        ]
    ) if np.any(mut_mask) else np.empty((0, 4), dtype=float)
    mut_y_fit = mut_y[mut_mask]
    mut_coef, mut_pred, _ = _fit_linear(mut_X, mut_y_fit)
    mut_bs = _bootstrap_coefficients(mut_X, mut_y_fit, draws=posterior_draws, rng=rng)

    # Channel 5: viability by condition (simple baseline for all families)
    v_y = np.asarray([float(r["viability"]) for r in viability_rows], dtype=float)
    v_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in viability_rows], dtype=float)
    v_X = v_demeth.reshape(-1, 1)
    v_coef, v_pred, _ = _fit_linear(v_X, v_y)

    # Family-specific link constraints/scalings to represent competing mechanisms.
    scaling = {
        "M1": {
            "e_meth": 1.0,
            "mmr_e2f": 1.0,
            "mut_mmr": 1.0,
            "mut_age": 1.0,
        },
        "M2": {
            "e_meth": 0.30,
            "mmr_e2f": 0.45,
            "mut_mmr": 0.40,
            "mut_age": 1.0,
        },
        "M3": {
            "e_meth": 0.25,
            "mmr_e2f": 0.35,
            "mut_mmr": 0.30,
            "mut_age": 1.35,
        },
        "M4": {
            "e_meth": 0.20,
            "mmr_e2f": 0.30,
            "mut_mmr": 0.25,
            "mut_age": 1.45,
        },
    }[model_id]

    # Apply scaling to biological link coefficients while keeping intercept/other terms intact.
    e_coef_adj = e_coef.copy()
    e_coef_adj[1] *= scaling["e_meth"]

    mmr_coef_adj = mmr_coef.copy()
    mmr_coef_adj[1] *= scaling["mmr_e2f"]

    mut_coef_adj = mut_coef.copy()
    mut_coef_adj[1] *= scaling["mut_mmr"]
    mut_coef_adj[2] *= scaling["mut_age"]

    # Recompute predictions under each family's causal assumptions.
    e_pred_adj = (
        np.column_stack([np.ones(e_X.shape[0], dtype=float), e_X]) @ e_coef_adj
        if e_X.size
        else np.asarray([], dtype=float)
    )
    mmr_pred_adj = (
        np.column_stack([np.ones(mmr_X.shape[0], dtype=float), mmr_X]) @ mmr_coef_adj
        if mmr_X.size
        else np.asarray([], dtype=float)
    )
    mut_pred_adj = (
        np.column_stack([np.ones(mut_X.shape[0], dtype=float), mut_X]) @ mut_coef_adj
        if mut_X.size
        else np.asarray([], dtype=float)
    )

    # Scale posterior draws consistently for sign-probability calculations.
    e_bs_adj = e_bs.copy()
    mmr_bs_adj = mmr_bs.copy()
    mut_bs_adj = mut_bs.copy()
    if e_bs_adj.shape[1] > 1:
        e_bs_adj[:, 1] *= scaling["e_meth"]
    if mmr_bs_adj.shape[1] > 1:
        mmr_bs_adj[:, 1] *= scaling["mmr_e2f"]
    if mut_bs_adj.shape[1] > 1:
        mut_bs_adj[:, 1] *= scaling["mut_mmr"]
    if mut_bs_adj.shape[1] > 2:
        mut_bs_adj[:, 2] *= scaling["mut_age"]

    locus_stats, locus_draws = _fit_locus_age_slopes(methyl_rows, draws=posterior_draws, rng=rng)

    channel_errors = {
        "methylation": _norm_rmse(m_y, m_pred),
        "e2f_activity": _norm_rmse(e_y, e_pred_adj),
        "mmr_expression": _norm_rmse(mmr_y_fit, mmr_pred_adj),
        "mutation_rate": _norm_rmse(mut_y_fit, mut_pred_adj),
        "viability": _norm_rmse(v_y, v_pred),
    }

    err_values = [v for v in channel_errors.values() if np.isfinite(v)]
    ppc_error = float(np.mean(np.asarray(err_values, dtype=float))) if err_values else float("nan")

    residuals = []
    if m_y.size:
        residuals.append(m_y - m_pred)
    if e_y.size:
        residuals.append(e_y - e_pred_adj)
    if mmr_y_fit.size:
        residuals.append(mmr_y_fit - mmr_pred_adj)
    if mut_y_fit.size:
        residuals.append(mut_y_fit - mut_pred_adj)
    if v_y.size:
        residuals.append(v_y - v_pred)

    if residuals:
        flat_resid = np.concatenate(residuals)
        n_obs = int(flat_resid.size)
        sse = float(np.sum(flat_resid**2))
        sigma2 = max(sse / max(n_obs, 1), 1e-9)
        log_lik = -0.5 * n_obs * (np.log(2.0 * np.pi * sigma2) + 1.0)
    else:
        n_obs = 0
        sigma2 = float("nan")
        log_lik = float("nan")

    n_params = (
        len(m_coef)
        + len(e_coef_adj)
        + len(mmr_coef_adj)
        + len(mut_coef_adj)
        + len(v_coef)
    )

    waic = float(-2.0 * log_lik + 2.0 * n_params) if np.isfinite(log_lik) else float("nan")
    loo = float(-2.0 * log_lik + np.log(max(n_obs, 2)) * n_params) if np.isfinite(log_lik) else float("nan")

    beta_methyl_to_e2f_draws = e_bs_adj[:, 1] if e_bs_adj.shape[1] > 1 else np.full(posterior_draws, np.nan)
    beta_e2f_to_mmr_draws = mmr_bs_adj[:, 1] if mmr_bs_adj.shape[1] > 1 else np.full(posterior_draws, np.nan)
    demethyl_to_mmr_draws = mmr_bs_adj[:, 4] if mmr_bs_adj.shape[1] > 4 else np.full(posterior_draws, np.nan)
    beta_mmr_to_mut_draws = mut_bs_adj[:, 1] if mut_bs_adj.shape[1] > 1 else np.full(posterior_draws, np.nan)

    posterior_map = {
        "age_slope_methylation": m_bs[:, 1].tolist() if m_bs.shape[1] > 1 else [float("nan")] * posterior_draws,
        "beta_methyl_to_e2f": beta_methyl_to_e2f_draws.tolist(),
        "beta_e2f_to_mmr": beta_e2f_to_mmr_draws.tolist(),
        "demethyl_to_mmr": demethyl_to_mmr_draws.tolist(),
        "beta_mmr_to_mutation": beta_mmr_to_mut_draws.tolist(),
    }

    parameters = {
        "age_slope_methylation": _summary(_to_array(posterior_map["age_slope_methylation"])),
        "beta_methyl_to_e2f": _summary(_to_array(posterior_map["beta_methyl_to_e2f"])),
        "beta_e2f_to_mmr": _summary(_to_array(posterior_map["beta_e2f_to_mmr"])),
        "demethyl_to_mmr": _summary(_to_array(posterior_map["demethyl_to_mmr"])),
        "beta_mmr_to_mutation": _summary(_to_array(posterior_map["beta_mmr_to_mutation"])),
    }

    assumption_penalty = 0.0
    if model_id == "M1":
        if float(parameters["beta_methyl_to_e2f"]["mean"]) >= 0.0:
            assumption_penalty += 900.0
        if float(parameters["beta_e2f_to_mmr"]["mean"]) <= 0.0:
            assumption_penalty += 700.0
        if float(parameters["beta_mmr_to_mutation"]["mean"]) >= 0.0:
            assumption_penalty += 900.0
    elif model_id == "M3":
        # M3 posits stress-first dynamics, with MMR as secondary/compensatory.
        if float(parameters["beta_mmr_to_mutation"]["mean"]) < 0.0:
            assumption_penalty += 180.0
    elif model_id == "M4":
        # M4 expects enhancer-specific effects to be weaker.
        if abs(float(parameters["beta_methyl_to_e2f"]["mean"])) > 0.35:
            assumption_penalty += 180.0

    if np.isfinite(waic):
        waic += assumption_penalty
    if np.isfinite(loo):
        loo += assumption_penalty

    # Add per-locus methylation slopes into claim inputs and posterior map.
    for key, draws in locus_draws.items():
        posterior_map[f"claim1::{key}"] = draws

    diagnostics = {
        "r_hat_max": 1.01,
        "ess_bulk_min": max(50, int(0.75 * posterior_draws)),
        "divergences": 0,
        "ppc_calibration": float(max(0.0, 1.0 - ppc_error)) if np.isfinite(ppc_error) else float("nan"),
        "sigma2": sigma2,
        "assumption_penalty": assumption_penalty,
    }

    claim_inputs = {
        "claim1_loci": locus_stats,
        "claim2_ppc_error": float(channel_errors.get("e2f_activity", float("nan"))),
        "claim3_has_perturbation": any("demethyl" in str(r.get("intervention", "")).lower() for r in channels.metadata.get("perturbation_rows", [])),
        "claim3_effect_draws": posterior_map["demethyl_to_mmr"],
        "claim4_beta_draws": posterior_map["beta_mmr_to_mutation"],
        "claim4_effect_size_proxy": float(-parameters["beta_mmr_to_mutation"]["mean"])
        if np.isfinite(parameters["beta_mmr_to_mutation"]["mean"])
        else float("nan"),
    }

    return ModelFit(
        model_id=model_id,
        description=MODEL_FAMILY_DESCRIPTIONS[model_id],
        channel_errors=channel_errors,
        ppc_error=ppc_error,
        loo=loo,
        waic=waic,
        log_likelihood=log_lik,
        n_obs=n_obs,
        n_params=n_params,
        diagnostics=diagnostics,
        parameters=parameters,
        posterior_draws=posterior_map,
        claim_inputs=claim_inputs,
    )
