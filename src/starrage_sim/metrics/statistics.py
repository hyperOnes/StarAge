from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable

import numpy as np


def quantile_ci(values: np.ndarray | list[float], alpha: float = 0.05) -> tuple[float, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan")
    lower = float(np.quantile(arr, alpha / 2.0))
    upper = float(np.quantile(arr, 1.0 - alpha / 2.0))
    return lower, upper


def summarize(values: np.ndarray | list[float], alpha: float = 0.05) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {
            "mean": float("nan"),
            "median": float("nan"),
            "std": float("nan"),
            "n": 0,
            "ci_low": float("nan"),
            "ci_high": float("nan"),
        }
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {
            "mean": float("nan"),
            "median": float("nan"),
            "std": float("nan"),
            "n": int(arr.size),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
        }
    ci_low, ci_high = quantile_ci(finite, alpha=alpha)
    return {
        "mean": float(np.mean(finite)),
        "median": float(np.median(finite)),
        "std": float(np.std(finite, ddof=1)) if finite.size > 1 else 0.0,
        "n": int(arr.size),
        "ci_low": ci_low,
        "ci_high": ci_high,
    }


def sem(std: float, n: int) -> float:
    if n <= 0:
        return float("nan")
    return float(std / np.sqrt(max(n, 1)))


def deterministic_seed(*parts: object) -> int:
    payload = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) & 0x7FFFFFFF


def finite_fraction(values: Iterable[float]) -> float:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.isfinite(arr).sum() / arr.size)


def proportion_ci_wilson(
    successes: int,
    n: int,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    if n <= 0:
        return float("nan"), float("nan")

    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n)))
    return float(max(0.0, center - half)), float(min(1.0, center + half))
