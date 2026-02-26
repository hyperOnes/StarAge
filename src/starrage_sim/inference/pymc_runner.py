from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import contextlib
import io
import os
import tempfile
from typing import Any
import warnings
import logging

import numpy as np

from starrage_sim.config import SimulationConfig
from starrage_sim.data.loaders import ScienceDataBundle
from starrage_sim.model.causal_graph import CausalChannels, build_causal_channels
from starrage_sim.model.competing_models import MODEL_FAMILY_DESCRIPTIONS, fit_competing_model


def _ensure_runtime_cache_dirs() -> None:
    tmp_root = Path(tempfile.gettempdir()) / "starrage_sim_runtime"
    mpl_dir = tmp_root / "mplconfig"
    pytensor_dir = tmp_root / "pytensor"
    mpl_dir.mkdir(parents=True, exist_ok=True)
    pytensor_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLCONFIGDIR", str(mpl_dir))

    pytensor_flags = os.environ.get("PYTENSOR_FLAGS", "").strip()
    if "base_compiledir=" not in pytensor_flags:
        if pytensor_flags:
            os.environ["PYTENSOR_FLAGS"] = f"{pytensor_flags},base_compiledir={pytensor_dir}"
        else:
            os.environ["PYTENSOR_FLAGS"] = f"base_compiledir={pytensor_dir}"


_ensure_runtime_cache_dirs()

logging.getLogger("pymc").setLevel(logging.ERROR)
logging.getLogger("pytensor").setLevel(logging.ERROR)

try:
    import pymc as _pm  # type: ignore # pragma: no cover

    HAS_PYMC = True
except Exception:  # pragma: no cover - optional dependency
    HAS_PYMC = False

try:
    import arviz as _az  # type: ignore # pragma: no cover

    HAS_ARVIZ = True
except Exception:  # pragma: no cover - optional dependency
    HAS_ARVIZ = False


MODEL_SCALING: dict[str, dict[str, float]] = {
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
}


@dataclass(frozen=True)
class ChannelData:
    m_age: np.ndarray
    m_demeth: np.ndarray
    m_y: np.ndarray
    e_meth: np.ndarray
    e_age: np.ndarray
    e_y: np.ndarray
    r_e2f: np.ndarray
    r_age: np.ndarray
    r_phase: np.ndarray
    r_demeth: np.ndarray
    r_y: np.ndarray
    u_mmr: np.ndarray
    u_age: np.ndarray
    u_viability: np.ndarray
    u_demeth: np.ndarray
    u_y: np.ndarray
    v_demeth: np.ndarray
    v_y: np.ndarray


@dataclass(frozen=True)
class PymcFitArtifacts:
    model_fit: dict[str, Any]
    advi_loss: float


def _infer_draw_count(config: SimulationConfig, runtime_profile: str) -> int:
    draws = int(config.inference.nuts_settings.get("draws", 300))
    if runtime_profile == "quick":
        return max(80, min(200, draws))
    return max(120, draws)


def _metric_value(model_fit: dict[str, Any], metric: str) -> float:
    value = model_fit.get(metric)
    if value is None or not np.isfinite(float(value)):
        return float("inf")
    return float(value)


def _rank_models(model_fits: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    ranked = sorted(
        model_fits,
        key=lambda m: _metric_value(m, metric),
    )
    return [
        {
            "model_id": row.get("model_id"),
            "metric": metric,
            "score": _metric_value(row, metric),
        }
        for row in ranked
    ]


def _filter_datasets(
    datasets: dict[str, list[dict[str, Any]]],
    donor_exclude: str | None = None,
    tissue_only: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}

    for key, rows in datasets.items():
        filtered: list[dict[str, Any]] = []
        for row in rows:
            donor = str(row.get("donor_id", ""))
            tissue = str(row.get("tissue", ""))
            if donor_exclude is not None and donor == donor_exclude:
                continue
            if tissue_only is not None and tissue != tissue_only:
                continue
            filtered.append(dict(row))
        out[key] = filtered
    return out


def _condition_demethyl_flag(condition: str) -> int:
    return 1 if "demethyl" in condition.lower() else 0


@contextlib.contextmanager
def _quiet_pymc() -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


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
    if y.size == 0 or pred.size == 0 or y.size != pred.size:
        return float("nan")
    rmse = np.sqrt(np.mean((y - pred) ** 2))
    return float(rmse / (np.std(y) + 1e-9))


def _channel_data(channels: CausalChannels) -> ChannelData:
    methyl_rows = channels.methylation_rows
    e2f_rows = channels.e2f_rows
    mmr_rows = channels.mmr_rows
    mut_rows = channels.mutation_rows
    viability_rows = channels.viability_rows

    m_age = np.asarray([float(r["age"]) for r in methyl_rows], dtype=float)
    m_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in methyl_rows], dtype=float)
    m_y = np.asarray([float(r["methyl_beta"]) for r in methyl_rows], dtype=float)

    e_rows = [r for r in e2f_rows if np.isfinite(float(r.get("enhancer_methyl_beta", float("nan"))))]
    e_meth = np.asarray([float(r["enhancer_methyl_beta"]) for r in e_rows], dtype=float)
    e_age = np.asarray([float(r["age"]) for r in e_rows], dtype=float)
    e_y = np.asarray([float(r["e2f_signal"]) for r in e_rows], dtype=float)

    r_e2f_all = np.asarray([float(r.get("e2f_activity", float("nan"))) for r in mmr_rows], dtype=float)
    r_age_all = np.asarray([float(r["age"]) for r in mmr_rows], dtype=float)
    r_phase_all = np.asarray([1.0 if int(r.get("is_s_phase", 0)) == 1 else 0.0 for r in mmr_rows], dtype=float)
    r_demeth_all = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in mmr_rows], dtype=float)
    r_y_all = np.asarray([float(r["expr_norm"]) for r in mmr_rows], dtype=float)
    r_mask = np.isfinite(r_e2f_all) & np.isfinite(r_y_all)

    r_e2f = r_e2f_all[r_mask]
    r_age = r_age_all[r_mask]
    r_phase = r_phase_all[r_mask]
    r_demeth = r_demeth_all[r_mask]
    r_y = r_y_all[r_mask]

    u_mmr_all = np.asarray([float(r.get("mmr_sphase_expr", float("nan"))) for r in mut_rows], dtype=float)
    u_age_all = np.asarray([float(r["age"]) for r in mut_rows], dtype=float)
    u_v_all = np.asarray([float(r.get("viability", float("nan"))) for r in mut_rows], dtype=float)
    if np.any(np.isfinite(u_v_all)):
        u_v_all = np.where(np.isfinite(u_v_all), u_v_all, np.nanmean(u_v_all))
    else:
        u_v_all = np.full(u_v_all.shape, 0.90, dtype=float)

    u_demeth_all = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in mut_rows], dtype=float)
    u_y_all = np.asarray([float(r["mutations_per_division"]) for r in mut_rows], dtype=float)
    u_mask = np.isfinite(u_mmr_all) & np.isfinite(u_y_all)

    u_mmr = u_mmr_all[u_mask]
    u_age = u_age_all[u_mask]
    u_viability = u_v_all[u_mask]
    u_demeth = u_demeth_all[u_mask]
    u_y = u_y_all[u_mask]

    v_y = np.asarray([float(r["viability"]) for r in viability_rows], dtype=float)
    v_demeth = np.asarray([_condition_demethyl_flag(str(r.get("condition", ""))) for r in viability_rows], dtype=float)

    return ChannelData(
        m_age=m_age,
        m_demeth=m_demeth,
        m_y=m_y,
        e_meth=e_meth,
        e_age=e_age,
        e_y=e_y,
        r_e2f=r_e2f,
        r_age=r_age,
        r_phase=r_phase,
        r_demeth=r_demeth,
        r_y=r_y,
        u_mmr=u_mmr,
        u_age=u_age,
        u_viability=u_viability,
        u_demeth=u_demeth,
        u_y=u_y,
        v_demeth=v_demeth,
        v_y=v_y,
    )


def _build_pymc_model(channel: ChannelData, model_id: str) -> Any:
    if model_id not in MODEL_SCALING:
        raise ValueError(f"Unknown model family '{model_id}'")

    scaling = MODEL_SCALING[model_id]

    def _sigma(arr: np.ndarray) -> float:
        if arr.size == 0:
            return 1.0
        return float(max(np.std(arr), 1e-3))

    with _pm.Model() as model:
        # Methylation channel
        age_m = _pm.Data("age_m", (channel.m_age - 25.0) / 35.0)
        demeth_m = _pm.Data("demeth_m", channel.m_demeth)
        y_m = _pm.Data("y_m", channel.m_y)

        a_m = _pm.Normal("a_m", mu=float(np.mean(channel.m_y)), sigma=max(0.2, 2.0 * _sigma(channel.m_y)))
        b_m_age = _pm.Normal("b_m_age", mu=0.0, sigma=1.0)
        b_m_demeth = _pm.Normal("b_m_demeth", mu=0.0, sigma=1.0)
        sigma_m = _pm.HalfNormal("sigma_m", sigma=max(0.05, _sigma(channel.m_y)))
        mu_m = a_m + b_m_age * age_m + b_m_demeth * demeth_m
        _pm.Normal("obs_methyl", mu=mu_m, sigma=sigma_m, observed=y_m)

        # E2F channel
        meth_e = _pm.Data("meth_e", channel.e_meth)
        age_e = _pm.Data("age_e", (channel.e_age - 25.0) / 35.0)
        y_e = _pm.Data("y_e", channel.e_y)

        a_e = _pm.Normal("a_e", mu=float(np.mean(channel.e_y)), sigma=max(0.2, 2.0 * _sigma(channel.e_y)))
        b_e_meth = _pm.Normal("b_e_meth", mu=0.0, sigma=1.0)
        b_e_meth_eff = _pm.Deterministic("b_e_meth_eff", scaling["e_meth"] * b_e_meth)
        b_e_age = _pm.Normal("b_e_age", mu=0.0, sigma=1.0)
        sigma_e = _pm.HalfNormal("sigma_e", sigma=max(0.05, _sigma(channel.e_y)))
        mu_e = a_e + b_e_meth_eff * meth_e + b_e_age * age_e
        _pm.Normal("obs_e2f", mu=mu_e, sigma=sigma_e, observed=y_e)

        # MMR channel
        e2f_r = _pm.Data("e2f_r", channel.r_e2f)
        age_r = _pm.Data("age_r", (channel.r_age - 25.0) / 35.0)
        phase_r = _pm.Data("phase_r", channel.r_phase)
        demeth_r = _pm.Data("demeth_r", channel.r_demeth)
        y_r = _pm.Data("y_r", channel.r_y)

        a_r = _pm.Normal("a_r", mu=float(np.mean(channel.r_y)), sigma=max(0.2, 2.0 * _sigma(channel.r_y)))
        b_r_e2f = _pm.Normal("b_r_e2f", mu=0.0, sigma=1.0)
        b_r_e2f_eff = _pm.Deterministic("b_r_e2f_eff", scaling["mmr_e2f"] * b_r_e2f)
        b_r_age = _pm.Normal("b_r_age", mu=0.0, sigma=1.0)
        b_r_phase = _pm.Normal("b_r_phase", mu=0.0, sigma=1.0)
        b_r_demeth = _pm.Normal("b_r_demeth", mu=0.0, sigma=1.0)
        sigma_r = _pm.HalfNormal("sigma_r", sigma=max(0.05, _sigma(channel.r_y)))
        mu_r = a_r + b_r_e2f_eff * e2f_r + b_r_age * age_r + b_r_phase * phase_r + b_r_demeth * demeth_r
        _pm.Normal("obs_mmr", mu=mu_r, sigma=sigma_r, observed=y_r)

        # Mutation channel
        mmr_u = _pm.Data("mmr_u", channel.u_mmr)
        age_u = _pm.Data("age_u", (channel.u_age - 25.0) / 35.0)
        viability_u = _pm.Data("viability_u", channel.u_viability)
        demeth_u = _pm.Data("demeth_u", channel.u_demeth)
        y_u = _pm.Data("y_u", channel.u_y)

        a_u = _pm.Normal("a_u", mu=float(np.mean(channel.u_y)), sigma=max(0.2, 2.0 * _sigma(channel.u_y)))
        b_u_mmr = _pm.Normal("b_u_mmr", mu=0.0, sigma=1.0)
        b_u_mmr_eff = _pm.Deterministic("b_u_mmr_eff", scaling["mut_mmr"] * b_u_mmr)
        b_u_age = _pm.Normal("b_u_age", mu=0.0, sigma=1.0)
        b_u_age_eff = _pm.Deterministic("b_u_age_eff", scaling["mut_age"] * b_u_age)
        b_u_viability = _pm.Normal("b_u_viability", mu=0.0, sigma=1.0)
        b_u_demeth = _pm.Normal("b_u_demeth", mu=0.0, sigma=1.0)
        sigma_u = _pm.HalfNormal("sigma_u", sigma=max(0.05, _sigma(channel.u_y)))
        mu_u = a_u + b_u_mmr_eff * mmr_u + b_u_age_eff * age_u + b_u_viability * viability_u + b_u_demeth * demeth_u
        _pm.Normal("obs_mut", mu=mu_u, sigma=sigma_u, observed=y_u)

        # Viability channel
        demeth_v = _pm.Data("demeth_v", channel.v_demeth)
        y_v = _pm.Data("y_v", channel.v_y)

        a_v = _pm.Normal("a_v", mu=float(np.mean(channel.v_y)), sigma=max(0.05, 2.0 * _sigma(channel.v_y)))
        b_v_demeth = _pm.Normal("b_v_demeth", mu=0.0, sigma=1.0)
        sigma_v = _pm.HalfNormal("sigma_v", sigma=max(0.01, _sigma(channel.v_y)))
        mu_v = a_v + b_v_demeth * demeth_v
        _pm.Normal("obs_v", mu=mu_v, sigma=sigma_v, observed=y_v)

    return model


def _extract_draws_from_var(posterior: Any, var_name: str) -> list[float]:
    if var_name not in posterior.data_vars:
        return []
    arr = np.asarray(posterior[var_name], dtype=float)
    if arr.ndim < 2:
        return arr.reshape(-1).tolist()
    return arr.reshape(arr.shape[0] * arr.shape[1], -1)[:, 0].tolist()


def _diagnostics_from_idata(idata: Any) -> dict[str, Any]:
    diag: dict[str, Any] = {
        "r_hat_max": float("nan"),
        "ess_bulk_min": float("nan"),
        "divergences": 0,
        "ppc_calibration": float("nan"),
    }

    posterior = getattr(idata, "posterior", None)
    n_chains = 0
    try:
        if posterior is not None and hasattr(posterior, "sizes"):
            n_chains = int(posterior.sizes.get("chain", 0))
    except Exception:
        n_chains = 0

    if n_chains >= 2:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                rhat = _az.rhat(idata)
                if hasattr(rhat, "to_array"):
                    rh = np.asarray(rhat.to_array(), dtype=float).reshape(-1)
                    rh = rh[np.isfinite(rh)]
                    if rh.size:
                        diag["r_hat_max"] = float(np.max(rh))
        except Exception:
            pass

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                ess = _az.ess(idata, method="bulk")
                if hasattr(ess, "to_array"):
                    es = np.asarray(ess.to_array(), dtype=float).reshape(-1)
                    es = es[np.isfinite(es)]
                    if es.size:
                        diag["ess_bulk_min"] = float(np.min(es))
        except Exception:
            pass

    try:
        if hasattr(idata, "sample_stats") and "diverging" in idata.sample_stats.data_vars:
            div = np.asarray(idata.sample_stats["diverging"], dtype=float)
            diag["divergences"] = int(np.nansum(div))
    except Exception:
        pass

    return diag


def _scores_from_idata(idata: Any, fallback_loo: float, fallback_waic: float, fallback_ll: float) -> tuple[float, float, float, int, int]:
    loo_score = fallback_loo
    waic_score = fallback_waic
    log_lik = fallback_ll
    n_obs = 0

    try:
        loo = _az.loo(idata, pointwise=False)
        if hasattr(loo, "elpd_loo"):
            loo_score = float(-2.0 * float(loo.elpd_loo))
    except Exception:
        pass

    try:
        waic = _az.waic(idata, pointwise=False)
        if hasattr(waic, "elpd_waic"):
            waic_score = float(-2.0 * float(waic.elpd_waic))
    except Exception:
        pass

    try:
        if hasattr(idata, "log_likelihood"):
            total_ll = None
            n_obs_sum = 0
            for name in idata.log_likelihood.data_vars:
                arr = np.asarray(idata.log_likelihood[name], dtype=float)
                if arr.ndim < 2:
                    continue
                if arr.ndim == 2:
                    ll = arr
                    n_obs_var = 1
                else:
                    ll = arr.reshape(arr.shape[0], arr.shape[1], -1).sum(axis=2)
                    n_obs_var = int(np.prod(arr.shape[2:]))
                total_ll = ll if total_ll is None else (total_ll + ll)
                n_obs_sum += n_obs_var

            if total_ll is not None:
                log_lik = float(np.mean(total_ll))
                n_obs = n_obs_sum
    except Exception:
        pass

    n_params = 0
    try:
        if hasattr(idata, "posterior"):
            for name in idata.posterior.data_vars:
                arr = np.asarray(idata.posterior[name])
                if arr.ndim < 2:
                    continue
                n_params += int(np.prod(arr.shape[2:])) if arr.ndim > 2 else 1
    except Exception:
        pass

    return loo_score, waic_score, log_lik, n_obs, n_params


def _channel_errors_from_ppc(model: Any, idata: Any, channel: ChannelData, seed: int) -> tuple[dict[str, float], float]:
    try:
        with model:
            with _quiet_pymc():
                ppc = _pm.sample_posterior_predictive(
                    idata,
                    var_names=["obs_methyl", "obs_e2f", "obs_mmr", "obs_mut", "obs_v"],
                    progressbar=False,
                    random_seed=seed,
                    return_inferencedata=True,
                )

        pred_m = np.asarray(ppc.posterior_predictive["obs_methyl"], dtype=float).mean(axis=(0, 1))
        pred_e = np.asarray(ppc.posterior_predictive["obs_e2f"], dtype=float).mean(axis=(0, 1))
        pred_r = np.asarray(ppc.posterior_predictive["obs_mmr"], dtype=float).mean(axis=(0, 1))
        pred_u = np.asarray(ppc.posterior_predictive["obs_mut"], dtype=float).mean(axis=(0, 1))
        pred_v = np.asarray(ppc.posterior_predictive["obs_v"], dtype=float).mean(axis=(0, 1))

        channel_errors = {
            "methylation": _norm_rmse(channel.m_y, pred_m),
            "e2f_activity": _norm_rmse(channel.e_y, pred_e),
            "mmr_expression": _norm_rmse(channel.r_y, pred_r),
            "mutation_rate": _norm_rmse(channel.u_y, pred_u),
            "viability": _norm_rmse(channel.v_y, pred_v),
        }
        vals = [v for v in channel_errors.values() if np.isfinite(v)]
        ppc_error = float(np.mean(np.asarray(vals, dtype=float))) if vals else float("nan")
        return channel_errors, ppc_error
    except Exception:
        return {}, float("nan")


def _fit_single_model_pymc(
    model_id: str,
    channels: CausalChannels,
    draws: int,
    seed: int,
    config: SimulationConfig,
    use_nuts: bool,
) -> PymcFitArtifacts:
    base = fit_competing_model(
        model_id=model_id,
        channels=channels,
        posterior_draws=max(draws, 80),
        seed=seed,
    ).to_dict()

    if not (HAS_PYMC and HAS_ARVIZ):
        base["diagnostics"]["sampler"] = "surrogate"
        return PymcFitArtifacts(model_fit=base, advi_loss=float("inf"))

    channel = _channel_data(channels)
    if (
        channel.m_y.size < 4
        or channel.e_y.size < 4
        or channel.r_y.size < 4
        or channel.u_y.size < 4
        or channel.v_y.size < 2
    ):
        base["diagnostics"]["sampler"] = "surrogate_insufficient_rows"
        return PymcFitArtifacts(model_fit=base, advi_loss=float("inf"))

    try:
        model = _build_pymc_model(channel=channel, model_id=model_id)

        vi = config.inference.vi_settings
        nuts = config.inference.nuts_settings

        advi_steps = int(vi.get("advi_steps", 2000))
        advi_posterior_draws = int(vi.get("posterior_draws", max(200, draws)))

        with model:
            with _quiet_pymc():
                approx = _pm.fit(
                    n=advi_steps,
                    method="advi",
                    random_seed=seed,
                    progressbar=False,
                    callbacks=[],
                )
                idata_advi = approx.sample(draws=advi_posterior_draws, random_seed=seed)

        advi_loss = float(approx.hist[-1]) if hasattr(approx, "hist") and len(approx.hist) > 0 else float("nan")

        idata = idata_advi
        sampler = "advi"

        if use_nuts:
            nuts_draws = int(min(max(80, draws), int(nuts.get("draws", draws))))
            nuts_tune = int(nuts.get("tune", nuts_draws))
            if config.runtime_profile == "quick":
                nuts_tune = int(min(max(80, nuts_tune), 200))
            target_accept = float(nuts.get("target_accept", 0.90))
            chains = int(nuts.get("chains", 2))
            chains = max(1, min(chains, 2))

            with model:
                with _quiet_pymc():
                    idata = _pm.sample(
                        draws=nuts_draws,
                        tune=nuts_tune,
                        chains=chains,
                        cores=1,
                        target_accept=target_accept,
                        random_seed=seed,
                        progressbar=False,
                        init="advi+adapt_diag",
                        compute_convergence_checks=False,
                        idata_kwargs={"log_likelihood": True},
                    )
            sampler = "advi+nuts"
        else:
            try:
                with model:
                    with _quiet_pymc():
                        idata = _pm.compute_log_likelihood(idata, progressbar=False)
            except Exception:
                pass

        posterior = idata.posterior
        draws_map = {
            "age_slope_methylation": _extract_draws_from_var(posterior, "b_m_age"),
            "beta_methyl_to_e2f": _extract_draws_from_var(posterior, "b_e_meth_eff"),
            "beta_e2f_to_mmr": _extract_draws_from_var(posterior, "b_r_e2f_eff"),
            "demethyl_to_mmr": _extract_draws_from_var(posterior, "b_r_demeth"),
            "beta_mmr_to_mutation": _extract_draws_from_var(posterior, "b_u_mmr_eff"),
        }

        for key, values in draws_map.items():
            if not values:
                draws_map[key] = list(base["posterior_draws"].get(key, []))

        params_map = {
            key: _summary(np.asarray(values, dtype=float)) for key, values in draws_map.items()
        }

        loo_score, waic_score, log_lik, n_obs, n_params = _scores_from_idata(
            idata,
            fallback_loo=float(base.get("loo", float("nan"))),
            fallback_waic=float(base.get("waic", float("nan"))),
            fallback_ll=float(base.get("log_likelihood", float("nan"))),
        )

        channel_errors, ppc_error = _channel_errors_from_ppc(model=model, idata=idata, channel=channel, seed=seed)
        if not channel_errors:
            channel_errors = dict(base.get("channel_errors", {}))
            ppc_error = float(base.get("ppc_error", float("nan")))

        diag = dict(base.get("diagnostics", {}))
        diag.update(_diagnostics_from_idata(idata))
        diag["sampler"] = sampler
        diag["advi_loss_final"] = advi_loss
        diag["ppc_calibration"] = float(max(0.0, 1.0 - ppc_error)) if np.isfinite(ppc_error) else float("nan")

        base["posterior_draws"].update(draws_map)
        base["parameters"].update(params_map)
        base["diagnostics"] = diag
        base["channel_errors"] = channel_errors
        base["ppc_error"] = ppc_error
        base["loo"] = loo_score
        base["waic"] = waic_score
        base["log_likelihood"] = log_lik
        if n_obs > 0:
            base["n_obs"] = n_obs
        if n_params > 0:
            base["n_params"] = n_params

        claim_inputs = dict(base.get("claim_inputs", {}))
        claim_inputs["claim2_ppc_error"] = float(channel_errors.get("e2f_activity", ppc_error))
        claim_inputs["claim3_effect_draws"] = list(draws_map["demethyl_to_mmr"])
        claim_inputs["claim4_beta_draws"] = list(draws_map["beta_mmr_to_mutation"])
        claim_inputs["claim4_effect_size_proxy"] = (
            float(-params_map["beta_mmr_to_mutation"]["mean"])
            if np.isfinite(float(params_map["beta_mmr_to_mutation"]["mean"]))
            else float(claim_inputs.get("claim4_effect_size_proxy", float("nan")))
        )
        base["claim_inputs"] = claim_inputs

        return PymcFitArtifacts(model_fit=base, advi_loss=advi_loss)
    except Exception as exc:
        base["diagnostics"]["sampler"] = "surrogate_after_pymc_error"
        base["diagnostics"]["pymc_error"] = str(exc)
        return PymcFitArtifacts(model_fit=base, advi_loss=float("inf"))


def _fit_model_set(
    model_ids: list[str],
    channels: Any,
    draws: int,
    seed: int,
    config: SimulationConfig,
    runtime_profile: str,
    backend: str,
) -> list[dict[str, Any]]:
    if backend == "surrogate":
        fits: list[dict[str, Any]] = []
        for idx, model_id in enumerate(model_ids):
            fit = fit_competing_model(
                model_id=model_id,
                channels=channels,
                posterior_draws=draws,
                seed=int(seed + idx * 1009),
            )
            fits.append(fit.to_dict())
        return fits

    if backend == "pymc_advi_only":
        fits: list[dict[str, Any]] = []
        for idx, model_id in enumerate(model_ids):
            artifacts = _fit_single_model_pymc(
                model_id=model_id,
                channels=channels,
                draws=draws,
                seed=int(seed + idx * 1009),
                config=config,
                use_nuts=False,
            )
            fits.append(artifacts.model_fit)
        return fits

    if backend != "pymc":
        raise ValueError(f"Unknown fit backend mode '{backend}'")

    # Phase 1: ADVI for all models.
    advi_results: dict[str, PymcFitArtifacts] = {}
    advi_rank_rows: list[tuple[str, float]] = []
    for idx, model_id in enumerate(model_ids):
        artifacts = _fit_single_model_pymc(
            model_id=model_id,
            channels=channels,
            draws=draws,
            seed=int(seed + idx * 1009),
            config=config,
            use_nuts=False,
        )
        advi_results[model_id] = artifacts
        advi_rank_rows.append((model_id, float(artifacts.advi_loss)))

    advi_rank_rows.sort(key=lambda x: x[1])

    # Phase 2: NUTS calibration by runtime profile.
    if runtime_profile == "quick":
        selected_for_nuts = [row[0] for row in advi_rank_rows[: min(2, len(advi_rank_rows))]]
        # Always calibrate M1 with NUTS in quick mode since it is the primary mechanism claim target.
        if "M1" in model_ids and "M1" not in selected_for_nuts:
            selected_for_nuts.append("M1")
    else:
        selected_for_nuts = [row[0] for row in advi_rank_rows]

    outputs: dict[str, dict[str, Any]] = {mid: advi_results[mid].model_fit for mid in model_ids}

    for model_id in selected_for_nuts:
        idx = model_ids.index(model_id)
        artifacts = _fit_single_model_pymc(
            model_id=model_id,
            channels=channels,
            draws=draws,
            seed=int(seed + idx * 1009 + 707),
            config=config,
            use_nuts=True,
        )
        outputs[model_id] = artifacts.model_fit

    return [outputs[mid] for mid in model_ids]


def fit_models(
    config: SimulationConfig,
    bundle: ScienceDataBundle,
    runtime_profile: str = "quick",
) -> dict[str, Any]:
    model_ids = [m for m in config.mechanism_models.enabled_families if m in MODEL_FAMILY_DESCRIPTIONS]
    if not model_ids:
        raise ValueError("No supported mechanism models were enabled")

    draws = _infer_draw_count(config, runtime_profile)
    channels = build_causal_channels(bundle.datasets)
    channels.metadata["perturbation_rows"] = bundle.datasets.get("perturbation_events", [])

    backend_requested = str(config.inference.backend).lower()
    use_pymc_backend = backend_requested == "pymc" and HAS_PYMC and HAS_ARVIZ

    model_fits = _fit_model_set(
        model_ids=model_ids,
        channels=channels,
        draws=draws,
        seed=config.seed,
        config=config,
        runtime_profile=runtime_profile,
        backend="pymc" if use_pymc_backend else "surrogate",
    )

    metric = config.inference.comparison_metric
    ranked = _rank_models(model_fits, metric=metric)
    top_model = ranked[0]["model_id"] if ranked else None

    donors = sorted(
        {
            str(row.get("donor_id"))
            for rows in bundle.datasets.values()
            for row in rows
            if row.get("donor_id") is not None
        }
    )
    tissues = sorted(
        {
            str(row.get("tissue"))
            for rows in bundle.datasets.values()
            for row in rows
            if row.get("tissue") is not None
        }
    )

    sensitivity_requested = str(config.inference.sensitivity_backend).lower()
    if use_pymc_backend and sensitivity_requested == "advi":
        sensitivity_backend = "pymc_advi_only"
    else:
        sensitivity_backend = "surrogate"

    lo_do_runs: list[dict[str, Any]] = []
    for donor in donors[: min(12, len(donors))]:
        subset = _filter_datasets(bundle.datasets, donor_exclude=donor)
        sub_channels = build_causal_channels(subset)
        sub_channels.metadata["perturbation_rows"] = subset.get("perturbation_events", [])
        sub_models = _fit_model_set(
            model_ids=model_ids,
            channels=sub_channels,
            draws=max(50, draws // 3),
            seed=config.seed + 91,
            config=config,
            runtime_profile="quick",
            backend=sensitivity_backend,
        )
        sub_rank = _rank_models(sub_models, metric=metric)
        lo_do_runs.append(
            {
                "left_out_donor": donor,
                "ranking": sub_rank,
                "top_model": sub_rank[0]["model_id"] if sub_rank else None,
            }
        )

    tissue_runs: list[dict[str, Any]] = []
    for tissue in tissues:
        subset = _filter_datasets(bundle.datasets, tissue_only=tissue)
        sub_channels = build_causal_channels(subset)
        sub_channels.metadata["perturbation_rows"] = subset.get("perturbation_events", [])
        sub_models = _fit_model_set(
            model_ids=model_ids,
            channels=sub_channels,
            draws=max(50, draws // 3),
            seed=config.seed + 121,
            config=config,
            runtime_profile="quick",
            backend=sensitivity_backend,
        )
        sub_rank = _rank_models(sub_models, metric=metric)
        tissue_runs.append(
            {
                "tissue": tissue,
                "ranking": sub_rank,
                "top_model": sub_rank[0]["model_id"] if sub_rank else None,
            }
        )

    lodo_stability = (
        float(np.mean([1.0 if r.get("top_model") == top_model else 0.0 for r in lo_do_runs]))
        if lo_do_runs
        else float("nan")
    )
    tissue_stability = (
        float(np.mean([1.0 if r.get("top_model") == top_model else 0.0 for r in tissue_runs]))
        if tissue_runs
        else float("nan")
    )

    backend_used = "numpy_fallback"
    if use_pymc_backend:
        samplers = [
            str(model.get("diagnostics", {}).get("sampler", ""))
            for model in model_fits
        ]
        if any("advi+nuts" in sampler for sampler in samplers):
            backend_used = "pymc_advi_nuts"
        elif any("advi" in sampler for sampler in samplers):
            backend_used = "pymc_advi_only"
        else:
            backend_used = "pymc_failed_surrogate_fallback"

    perturbation_targets = sorted(
        {
            str(row.get("target_locus"))
            for row in bundle.datasets.get("perturbation_events", [])
            if row.get("target_locus") is not None
        }
    )

    return {
        "backend_requested": backend_requested,
        "backend_used": backend_used,
        "runtime_profile": runtime_profile,
        "comparison_metric": metric,
        "models": model_fits,
        "ranking": ranked,
        "top_model": top_model,
        "channels": channels.to_dict(),
        "dataset_modes": dict(bundle.dataset_modes),
        "data_mode": bundle.mode,
        "has_real_data": bundle.has_real_data,
        "is_synthetic_only": bundle.is_synthetic_only,
        "perturbation_targets": perturbation_targets,
        "validation_report": bundle.validation_report,
        "sensitivity_backend_requested": sensitivity_requested,
        "sensitivity_backend_used": sensitivity_backend,
        "sensitivity": {
            "leave_one_donor_out": lo_do_runs,
            "by_tissue": tissue_runs,
            "top_model_stability": {
                "leave_one_donor_out": lodo_stability,
                "by_tissue": tissue_stability,
            },
        },
    }
