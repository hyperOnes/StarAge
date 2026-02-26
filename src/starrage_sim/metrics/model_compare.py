from __future__ import annotations

from typing import Any

import numpy as np


def _extract_score(model: dict[str, Any], metric: str) -> float:
    value = model.get(metric)
    if value is None:
        return float("inf")
    v = float(value)
    if not np.isfinite(v):
        return float("inf")
    return v


def compare_models(fit_payload: dict[str, Any], metric: str | None = None) -> dict[str, Any]:
    selected_metric = (metric or str(fit_payload.get("comparison_metric", "loo"))).lower()
    if selected_metric not in {"loo", "waic"}:
        selected_metric = "loo"

    models = fit_payload.get("models", [])
    ranked_raw = sorted(models, key=lambda m: _extract_score(m, selected_metric))

    if not ranked_raw:
        return {
            "metric": selected_metric,
            "ranking": [],
            "top_model": None,
            "diagnostics": {"reason": "no_models"},
        }

    top_score = _extract_score(ranked_raw[0], selected_metric)

    rows: list[dict[str, Any]] = []
    raw_weights: list[float] = []
    for idx, model in enumerate(ranked_raw, start=1):
        score = _extract_score(model, selected_metric)
        delta = float(score - top_score)
        weight = float(np.exp(-0.5 * delta)) if np.isfinite(delta) else 0.0
        raw_weights.append(weight)
        rows.append(
            {
                "rank": idx,
                "model_id": model.get("model_id"),
                "score": score,
                "delta": delta,
                "ppc_error": model.get("ppc_error"),
                "waic": model.get("waic"),
                "loo": model.get("loo"),
                "diagnostics": model.get("diagnostics", {}),
            }
        )

    weight_sum = float(np.sum(np.asarray(raw_weights, dtype=float)))
    for idx, row in enumerate(rows):
        row["weight"] = float(raw_weights[idx] / weight_sum) if weight_sum > 0 else 0.0

    top_model = str(rows[0]["model_id"])
    margin_vs: dict[str, float] = {}
    for row in rows[1:]:
        margin_vs[str(row["model_id"])] = float(row["score"] - rows[0]["score"])

    return {
        "metric": selected_metric,
        "ranking": rows,
        "top_model": top_model,
        "top_score": float(rows[0]["score"]),
        "top_margin_vs_competitors": margin_vs,
        "stability": fit_payload.get("sensitivity", {}).get("top_model_stability", {}),
        "backend": {
            "requested": fit_payload.get("backend_requested"),
            "used": fit_payload.get("backend_used"),
        },
    }
