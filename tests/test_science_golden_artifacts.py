from __future__ import annotations

import json
import math
from pathlib import Path
from numbers import Real

from starrage_sim.config import load_config
from starrage_sim.engine.science_pipeline import run_science_pipeline
from tests.science_utils import build_real_dataset, write_science_config


def _prepare_golden_config(cfg_path: Path) -> None:
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["runtime_profile"] = "quick"
    raw.setdefault("inference", {})["backend"] = "surrogate"
    raw["inference"]["sensitivity_backend"] = "surrogate"
    raw["inference"]["comparison_metric"] = "loo"
    raw["science_mode"]["data_mode"] = "data_required"
    raw["science_data"]["allow_synthetic_fallback"] = False
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")


def _assert_semantic_equal(actual: object, expected: object, path: str = "$") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"type mismatch at {path}: expected dict"
        assert set(actual.keys()) == set(expected.keys()), f"key mismatch at {path}"
        for key in sorted(expected.keys()):
            _assert_semantic_equal(actual[key], expected[key], f"{path}.{key}")
        return

    if isinstance(expected, list):
        assert isinstance(actual, list), f"type mismatch at {path}: expected list"
        assert len(actual) == len(expected), f"length mismatch at {path}"
        for idx, (a_item, e_item) in enumerate(zip(actual, expected, strict=True)):
            _assert_semantic_equal(a_item, e_item, f"{path}[{idx}]")
        return

    if isinstance(expected, bool) or expected is None:
        assert actual is expected, f"value mismatch at {path}"
        return

    if isinstance(expected, Real) and isinstance(actual, Real):
        e = float(expected)
        a = float(actual)
        if math.isnan(e):
            assert math.isnan(a), f"NaN mismatch at {path}"
            return
        assert math.isclose(a, e, rel_tol=1e-9, abs_tol=1e-9), f"numeric mismatch at {path}: {a} != {e}"
        return

    assert actual == expected, f"value mismatch at {path}: {actual!r} != {expected!r}"


def test_golden_science_artifacts_snapshots(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("STARRAGE_GIT_COMMIT_OVERRIDE", "golden-commit")

    cfg_path = write_science_config(tmp_path / "science.json", data_mode="data_required", allow_synthetic_fallback=False)
    _prepare_golden_config(cfg_path)

    data_dir = tmp_path / "data"
    build_real_dataset(cfg_path, data_dir)

    config = load_config(cfg_path)
    out_dir = tmp_path / "out"
    run_science_pipeline(config=config, out_dir=out_dir, data_dir=data_dir, runtime_profile="quick")

    actual_model = json.loads((out_dir / "model_comparison.json").read_text(encoding="utf-8"))
    actual_claims = json.loads((out_dir / "science_claims_verdict.json").read_text(encoding="utf-8"))

    fixture_dir = Path(__file__).parent / "fixtures" / "golden"
    expected_model = json.loads((fixture_dir / "model_comparison.json").read_text(encoding="utf-8"))
    expected_claims = json.loads((fixture_dir / "science_claims_verdict.json").read_text(encoding="utf-8"))

    _assert_semantic_equal(actual_model, expected_model)
    _assert_semantic_equal(actual_claims, expected_claims)
