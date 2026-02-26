from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from numbers import Real
import subprocess
from typing import Any

from starrage_sim.config import SimulationConfig


SCIENCE_PROVENANCE_FIELDS: tuple[str, ...] = (
    "assumptions_hash",
    "science_data_hash",
    "git_commit",
    "config_hash",
    "seed",
    "schema_version",
    "preprocessing_version",
    "preprocessing_hash",
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(_stable_json(value))


def resolve_git_commit(start_dir: Path) -> str:
    override = os.environ.get("STARRAGE_GIT_COMMIT_OVERRIDE") or os.environ.get("STARRAGE_GIT_COMMIT")
    if override:
        return str(override)

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=start_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return str(proc.stdout.strip() or "unknown")
    except Exception:
        return "unknown"


def config_file_hash(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except Exception:
        return "unknown"


def build_science_provenance(
    config: SimulationConfig,
    science_data_hash: str,
    schema_version: str,
    preprocessing_version: str,
    preprocessing_hash: str,
) -> dict[str, Any]:
    return {
        "assumptions_hash": config.assumptions_hash(),
        "science_data_hash": science_data_hash,
        "git_commit": resolve_git_commit(config.source_path.parent),
        "config_hash": config_file_hash(config.source_path),
        "seed": int(config.seed),
        "schema_version": str(schema_version),
        "preprocessing_version": str(preprocessing_version),
        "preprocessing_hash": str(preprocessing_hash),
    }


def extract_science_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    extracted = {k: payload.get(k) for k in SCIENCE_PROVENANCE_FIELDS}
    if all(v is None for v in extracted.values()) and isinstance(payload.get("provenance"), dict):
        p = payload["provenance"]
        extracted = {k: p.get(k) for k in SCIENCE_PROVENANCE_FIELDS}
    return extracted


def with_science_provenance(payload: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.update({k: provenance.get(k) for k in SCIENCE_PROVENANCE_FIELDS})
    out["provenance"] = {k: provenance.get(k) for k in SCIENCE_PROVENANCE_FIELDS}
    return out


def canonicalize_json_payload(value: Any, *, float_decimals: int = 10) -> Any:
    if isinstance(value, dict):
        return {str(k): canonicalize_json_payload(v, float_decimals=float_decimals) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize_json_payload(v, float_decimals=float_decimals) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = float(round(value, float_decimals))
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, Real):
        as_float = float(value)
        if math.isfinite(as_float):
            if float(as_float).is_integer():
                return int(round(as_float))
            rounded = float(round(as_float, float_decimals))
            return 0.0 if rounded == 0.0 else rounded
        return None
    return value
