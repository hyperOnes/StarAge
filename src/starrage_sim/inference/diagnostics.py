from __future__ import annotations

from typing import Any

import numpy as np


def posterior_summary_rows(fit_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in fit_payload.get("models", []):
        model_id = str(model.get("model_id", ""))
        params = model.get("parameters", {})
        for name, summary in params.items():
            rows.append(
                {
                    "model_id": model_id,
                    "parameter": name,
                    "mean": summary.get("mean"),
                    "sd": summary.get("sd"),
                    "hdi_low": summary.get("hdi_low"),
                    "hdi_high": summary.get("hdi_high"),
                    "p_gt_0": summary.get("p_gt_0"),
                    "p_lt_0": summary.get("p_lt_0"),
                }
            )
    rows.sort(key=lambda r: (str(r["model_id"]), str(r["parameter"])))
    return rows


def summarize_trace_diagnostics(fit_payload: dict[str, Any]) -> dict[str, Any]:
    per_model = []
    for model in fit_payload.get("models", []):
        diag = model.get("diagnostics", {})
        per_model.append(
            {
                "model_id": model.get("model_id"),
                "r_hat_max": diag.get("r_hat_max"),
                "ess_bulk_min": diag.get("ess_bulk_min"),
                "divergences": diag.get("divergences"),
                "ppc_calibration": diag.get("ppc_calibration"),
            }
        )

    r_hat_vals = [float(m["r_hat_max"]) for m in per_model if m.get("r_hat_max") is not None]
    ess_vals = [float(m["ess_bulk_min"]) for m in per_model if m.get("ess_bulk_min") is not None]
    div_vals = [float(m["divergences"]) for m in per_model if m.get("divergences") is not None]

    return {
        "backend_requested": fit_payload.get("backend_requested"),
        "backend_used": fit_payload.get("backend_used"),
        "runtime_profile": fit_payload.get("runtime_profile"),
        "per_model": per_model,
        "summary": {
            "r_hat_max": float(np.nanmax(np.asarray(r_hat_vals, dtype=float))) if r_hat_vals else float("nan"),
            "ess_bulk_min": float(np.nanmin(np.asarray(ess_vals, dtype=float))) if ess_vals else float("nan"),
            "total_divergences": float(np.nansum(np.asarray(div_vals, dtype=float))) if div_vals else 0.0,
        },
    }
