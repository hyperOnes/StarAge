from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - depends on environment
    yaml = None

from starrage_sim.model.types import SweepProfile


@dataclass(frozen=True)
class DoseAxis:
    min: float
    max: float
    n_coarse: int


@dataclass(frozen=True)
class RefineConfig:
    top_k: int
    step_fraction: float
    neighbor_radius: int


@dataclass(frozen=True)
class DoseRanges:
    amplification: DoseAxis
    cyclin_throttle: DoseAxis
    refine: RefineConfig


@dataclass(frozen=True)
class WashoutPrior:
    on_half_life: float
    off_half_life: float
    measurement_cv: float


@dataclass(frozen=True)
class TissueParams:
    alpha: float
    beta: float
    v0: float
    p1: float
    p2: float
    p3: float
    p4: float


@dataclass(frozen=True)
class GlobalNoise:
    mutation_noise_sigma: float
    viability_noise_sigma: float
    expression_noise_sigma: float
    stress_scale: float
    stress_penalty: float
    leakage_baseline: float
    leakage_noise_sigma: float


@dataclass(frozen=True)
class ObjectiveParams:
    synergy_k: float


@dataclass(frozen=True)
class WashoutDesign:
    n_timepoints: int
    measurement_cv_scale: float
    model_family: str
    holdout_fraction: float
    retention_horizon_divisions: float
    retention_min: float
    predictive_metric: str
    predictive_mae_max: float
    predictive_coverage_min: float
    gate_off_mark_required: bool


@dataclass(frozen=True)
class StrictGate:
    require_all_tissues: bool
    require_all_tissue_age: bool


@dataclass(frozen=True)
class ScienceMode:
    enabled: bool
    data_mode: str


@dataclass(frozen=True)
class ScienceData:
    data_dir: Path | None
    paths: dict[str, Path]
    allow_synthetic_fallback: bool


@dataclass(frozen=True)
class LocusSpec:
    tissue: str
    gene: str
    locus_id: str
    chrom: str | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class MechanismModels:
    enabled_families: list[str]


@dataclass(frozen=True)
class InferenceSettings:
    backend: str
    vi_settings: dict[str, Any]
    nuts_settings: dict[str, Any]
    comparison_metric: str
    sensitivity_backend: str


@dataclass(frozen=True)
class ClaimThresholds:
    posterior_probability: float
    claim1_delta_methylation_min: float
    claim1_min_rows_per_locus: int
    claim2_ppc_error_max: float
    claim3_effect_min: float
    claim4_mutation_reduction_min: float
    claim5_model_margin_min: float
    claim5_stability_min: float


@dataclass(frozen=True)
class VerdictPolicy:
    science_as_hard_blocker: bool
    allow_synthetic_science_pass_for_dev: bool


@dataclass
class SimulationConfig:
    source_path: Path
    seed: int
    runtime_profile: str
    tissues: list[str]
    donor_ages: list[int]
    dose_ranges: DoseRanges
    washout_priors: dict[str, WashoutPrior]
    viability_threshold: float
    mutation_reduction_target: float
    gate_rule: str
    fucci_threshold: float
    fucci_leakage_cap: float
    washout_r2_floor: float
    synergy_stability_min: float
    profiles: dict[str, SweepProfile]
    tissue_params: dict[str, TissueParams]
    baseline_mutation_rates: dict[str, dict[int, float]]
    global_noise: GlobalNoise
    objective: ObjectiveParams
    washout_design: WashoutDesign
    strict_gate: StrictGate
    sensitivity_configs: list[Path]
    science_mode: ScienceMode
    science_data: ScienceData
    locus_panel: list[LocusSpec]
    mechanism_models: MechanismModels
    inference: InferenceSettings
    claim_thresholds: ClaimThresholds
    verdict_policy: VerdictPolicy

    def resolved_profile(self, override: str | None = None) -> SweepProfile:
        key = override or self.runtime_profile
        if key not in self.profiles:
            raise KeyError(f"Unknown runtime profile '{key}'. Available: {sorted(self.profiles)}")
        return self.profiles[key]

    def assumptions_hash(self) -> str:
        payload = {
            "seed": self.seed,
            "runtime_profile": self.runtime_profile,
            "tissues": self.tissues,
            "donor_ages": self.donor_ages,
            "dose_ranges": {
                "amplification": self.dose_ranges.amplification.__dict__,
                "cyclin_throttle": self.dose_ranges.cyclin_throttle.__dict__,
                "refine": self.dose_ranges.refine.__dict__,
            },
            "washout_priors": {k: v.__dict__ for k, v in self.washout_priors.items()},
            "viability_threshold": self.viability_threshold,
            "mutation_reduction_target": self.mutation_reduction_target,
            "gate_rule": self.gate_rule,
            "fucci_threshold": self.fucci_threshold,
            "fucci_leakage_cap": self.fucci_leakage_cap,
            "washout_r2_floor": self.washout_r2_floor,
            "synergy_stability_min": self.synergy_stability_min,
            "profiles": {k: v.__dict__ for k, v in self.profiles.items()},
            "tissue_params": {k: v.__dict__ for k, v in self.tissue_params.items()},
            "baseline_mutation_rates": self.baseline_mutation_rates,
            "global_noise": self.global_noise.__dict__,
            "objective": self.objective.__dict__,
            "washout_design": self.washout_design.__dict__,
            "strict_gate": self.strict_gate.__dict__,
            "science_mode": self.science_mode.__dict__,
            "science_data": {
                "data_dir": str(self.science_data.data_dir) if self.science_data.data_dir else None,
                "paths": {k: str(v) for k, v in sorted(self.science_data.paths.items())},
                "allow_synthetic_fallback": self.science_data.allow_synthetic_fallback,
            },
            "locus_panel": [loc.__dict__ for loc in self.locus_panel],
            "mechanism_models": self.mechanism_models.__dict__,
            "inference": self.inference.__dict__,
            "claim_thresholds": self.claim_thresholds.__dict__,
            "verdict_policy": self.verdict_policy.__dict__,
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def config_hash(self) -> str:
        try:
            return hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        except Exception:
            return self.assumptions_hash()


class ConfigError(ValueError):
    pass


def _parse_dose_ranges(raw: dict[str, Any]) -> DoseRanges:
    amp = raw["amplification"]
    cyc = raw["cyclin_throttle"]
    ref = raw["refine"]
    return DoseRanges(
        amplification=DoseAxis(min=float(amp["min"]), max=float(amp["max"]), n_coarse=int(amp["n_coarse"])),
        cyclin_throttle=DoseAxis(min=float(cyc["min"]), max=float(cyc["max"]), n_coarse=int(cyc["n_coarse"])),
        refine=RefineConfig(
            top_k=int(ref["top_k"]),
            step_fraction=float(ref["step_fraction"]),
            neighbor_radius=int(ref["neighbor_radius"]),
        ),
    )


def _parse_profiles(raw: dict[str, Any]) -> dict[str, SweepProfile]:
    out: dict[str, SweepProfile] = {}
    for name, cfg in raw.items():
        out[name] = SweepProfile(
            name=name,
            replicates_per_condition=int(cfg["replicates_per_condition"]),
            n_divisions=int(cfg["n_divisions"]),
            fucci_cells=int(cfg["fucci_cells"]),
            bootstrap_n=int(cfg["bootstrap_n"]),
            workers=cfg.get("workers", "auto"),
        )
    return out


def _parse_baseline_rates(raw: dict[str, Any]) -> dict[str, dict[int, float]]:
    baseline: dict[str, dict[int, float]] = {}
    for tissue, by_age in raw.items():
        baseline[tissue] = {int(age): float(value) for age, value in by_age.items()}
    return baseline


def _resolve_optional_path(value: Any, cfg_path: Path) -> Path | None:
    if value in (None, "", False):
        return None
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = (cfg_path.parent / p).resolve()
    return p


def _parse_science_mode(raw: dict[str, Any]) -> ScienceMode:
    mode_raw = raw.get("science_mode", {}) or {}
    data_mode = str(mode_raw.get("data_mode", "schema_first")).strip().lower()
    if data_mode not in {"schema_first", "data_required", "synthetic_only"}:
        raise ConfigError(
            f"science_mode.data_mode must be one of schema_first|data_required|synthetic_only, got '{data_mode}'"
        )
    return ScienceMode(
        enabled=bool(mode_raw.get("enabled", True)),
        data_mode=data_mode,
    )


def _parse_science_data(raw: dict[str, Any], cfg_path: Path) -> ScienceData:
    src = raw.get("science_data", {}) or {}
    paths_src = src.get("paths", {}) or {}

    canonical_keys = [
        "methylation_observations",
        "e2f_activity_observations",
        "mmr_expression_observations",
        "mutation_observations",
        "perturbation_events",
        "viability_observations",
    ]

    paths: dict[str, Path] = {}
    for key in canonical_keys:
        value = paths_src.get(key, src.get(key))
        resolved = _resolve_optional_path(value, cfg_path)
        if resolved is not None:
            paths[key] = resolved

    return ScienceData(
        data_dir=_resolve_optional_path(src.get("data_dir"), cfg_path),
        paths=paths,
        allow_synthetic_fallback=bool(src.get("allow_synthetic_fallback", True)),
    )


def _parse_locus_panel(raw: dict[str, Any], tissues: list[str]) -> list[LocusSpec]:
    panel = raw.get("locus_panel", []) or []
    out: list[LocusSpec] = []
    for row in panel:
        out.append(
            LocusSpec(
                tissue=str(row["tissue"]),
                gene=str(row["gene"]),
                locus_id=str(row["locus_id"]),
                chrom=str(row.get("chrom")) if row.get("chrom") is not None else None,
                start=int(row["start"]) if row.get("start") is not None else None,
                end=int(row["end"]) if row.get("end") is not None else None,
            )
        )

    if out:
        return out

    # Defaults keep existing configs runnable while adding schema-first science mode.
    default_genes = ["MSH2", "MSH6", "MLH1", "PMS2"]
    for tissue in tissues:
        for idx, gene in enumerate(default_genes, start=1):
            out.append(LocusSpec(tissue=tissue, gene=gene, locus_id=f"{tissue}_{gene}_E{idx}"))
    return out


def _parse_mechanism_models(raw: dict[str, Any]) -> MechanismModels:
    mm = raw.get("mechanism_models", {}) or {}
    enabled = mm.get("enabled") or mm.get("enabled_families") or ["M1", "M2", "M3", "M4"]
    if not isinstance(enabled, list) or not enabled:
        raise ConfigError("mechanism_models.enabled_families must be a non-empty list")
    return MechanismModels(enabled_families=[str(x) for x in enabled])


def _parse_inference(raw: dict[str, Any]) -> InferenceSettings:
    inf = raw.get("inference", {}) or {}
    sensitivity_backend = str(inf.get("sensitivity_backend", "surrogate")).lower()
    if sensitivity_backend not in {"surrogate", "advi"}:
        raise ConfigError(
            f"inference.sensitivity_backend must be one of surrogate|advi, got '{sensitivity_backend}'"
        )
    return InferenceSettings(
        backend=str(inf.get("backend", "pymc")),
        vi_settings=dict(inf.get("vi_settings", {"advi_steps": 2000})),
        nuts_settings=dict(
            inf.get(
                "nuts_settings",
                {
                    "draws": 300,
                    "tune": 300,
                    "target_accept": 0.9,
                },
            )
        ),
        comparison_metric=str(inf.get("comparison_metric", "loo")).lower(),
        sensitivity_backend=sensitivity_backend,
    )


def _parse_claim_thresholds(raw: dict[str, Any]) -> ClaimThresholds:
    c = raw.get("claim_thresholds", {}) or {}
    c1 = c.get("claim1", {}) or {}
    c2 = c.get("claim2", {}) or {}
    c3 = c.get("claim3", {}) or {}
    c4 = c.get("claim4", {}) or {}
    c5 = c.get("claim5", {}) or {}

    return ClaimThresholds(
        posterior_probability=float(c.get("posterior_probability", c.get("posterior_prob", 0.95))),
        claim1_delta_methylation_min=float(c1.get("delta_methylation_min", c.get("claim1_delta_methylation_min", 0.04))),
        claim1_min_rows_per_locus=int(c1.get("min_rows_per_locus", c.get("claim1_min_rows_per_locus", 6))),
        claim2_ppc_error_max=float(c2.get("ppc_error_max", c.get("claim2_ppc_error_max", 0.35))),
        claim3_effect_min=float(c3.get("effect_min", c.get("claim3_effect_min", 0.03))),
        claim4_mutation_reduction_min=float(
            c4.get("mutation_reduction_min", c.get("claim4_mutation_reduction_min", 0.03))
        ),
        claim5_model_margin_min=float(c5.get("model_margin_min", c.get("claim5_model_margin_min", 2.0))),
        claim5_stability_min=float(c5.get("stability_min", c.get("claim5_stability_min", 0.70))),
    )


def _parse_verdict_policy(raw: dict[str, Any]) -> VerdictPolicy:
    vp = raw.get("verdict_policy", {}) or {}
    return VerdictPolicy(
        science_as_hard_blocker=bool(vp.get("science_as_hard_blocker", False)),
        allow_synthetic_science_pass_for_dev=bool(vp.get("allow_synthetic_science_pass_for_dev", False)),
    )


def load_config(path: str | Path) -> SimulationConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.exists():
        raise ConfigError(f"Config file does not exist: {cfg_path}")

    raw_text = cfg_path.read_text(encoding="utf-8")
    if yaml is not None:
        raw = yaml.safe_load(raw_text)
    else:
        # JSON is a strict subset of YAML, so JSON-formatted configs remain valid.
        raw = json.loads(raw_text)

    try:
        dose_ranges = _parse_dose_ranges(raw["dose_ranges"])
        profiles = _parse_profiles(raw["profiles"])
        baseline_mutation_rates = _parse_baseline_rates(raw["baseline_mutation_rates"])

        washout_priors = {
            tissue: WashoutPrior(
                on_half_life=float(values["on_half_life"]),
                off_half_life=float(values["off_half_life"]),
                measurement_cv=float(values["measurement_cv"]),
            )
            for tissue, values in raw["washout_priors"].items()
        }

        tissue_params = {
            tissue: TissueParams(
                alpha=float(values["alpha"]),
                beta=float(values["beta"]),
                v0=float(values["v0"]),
                p1=float(values["p1"]),
                p2=float(values["p2"]),
                p3=float(values["p3"]),
                p4=float(values["p4"]),
            )
            for tissue, values in raw["tissue_params"].items()
        }

        global_noise = GlobalNoise(
            mutation_noise_sigma=float(raw["global_noise"]["mutation_noise_sigma"]),
            viability_noise_sigma=float(raw["global_noise"]["viability_noise_sigma"]),
            expression_noise_sigma=float(raw["global_noise"]["expression_noise_sigma"]),
            stress_scale=float(raw["global_noise"]["stress_scale"]),
            stress_penalty=float(raw["global_noise"]["stress_penalty"]),
            leakage_baseline=float(raw["global_noise"]["leakage_baseline"]),
            leakage_noise_sigma=float(raw["global_noise"]["leakage_noise_sigma"]),
        )

        objective = ObjectiveParams(synergy_k=float(raw["objective"]["synergy_k"]))

        washout_raw = raw.get("washout_design", {})
        washout_design = WashoutDesign(
            n_timepoints=int(washout_raw.get("n_timepoints", 10)),
            measurement_cv_scale=float(washout_raw.get("measurement_cv_scale", 1.0)),
            model_family=str(washout_raw.get("model_family", "single_exponential_robust_logspace")),
            holdout_fraction=float(washout_raw.get("holdout_fraction", 0.25)),
            retention_horizon_divisions=float(washout_raw.get("retention_horizon_divisions", 80.0)),
            retention_min=float(washout_raw.get("retention_min", 0.70)),
            predictive_metric=str(washout_raw.get("predictive_metric", "predictive_coverage")),
            predictive_mae_max=float(
                washout_raw.get("predictive_mae_max", washout_raw.get("predictive_male_max", 0.20))
            ),
            predictive_coverage_min=float(washout_raw.get("predictive_coverage_min", 0.80)),
            gate_off_mark_required=bool(washout_raw.get("gate_off_mark_required", False)),
        )

        strict_raw = raw.get("strict_gate", {})
        strict_gate = StrictGate(
            require_all_tissues=bool(strict_raw.get("require_all_tissues", True)),
            require_all_tissue_age=bool(strict_raw.get("require_all_tissue_age", False)),
        )

        sensitivity_configs = []
        for item in raw.get("sensitivity_configs", []):
            sens_path = Path(item).expanduser()
            if not sens_path.is_absolute():
                sens_path = (cfg_path.parent / sens_path).resolve()
            sensitivity_configs.append(sens_path)

        tissues = [str(t) for t in raw["tissues"]]
        science_mode = _parse_science_mode(raw)
        science_data = _parse_science_data(raw, cfg_path)
        locus_panel = _parse_locus_panel(raw, tissues)
        mechanism_models = _parse_mechanism_models(raw)
        inference = _parse_inference(raw)
        claim_thresholds = _parse_claim_thresholds(raw)
        verdict_policy = _parse_verdict_policy(raw)

        return SimulationConfig(
            source_path=cfg_path,
            seed=int(raw["seed"]),
            runtime_profile=str(raw["runtime_profile"]),
            tissues=tissues,
            donor_ages=[int(a) for a in raw["donor_ages"]],
            dose_ranges=dose_ranges,
            washout_priors=washout_priors,
            viability_threshold=float(raw["viability_threshold"]),
            mutation_reduction_target=float(raw.get("mutation_reduction_target", 2.0)),
            gate_rule=str(raw.get("gate_rule", "confidence_bound_95")),
            fucci_threshold=float(raw["fucci_threshold"]),
            fucci_leakage_cap=float(raw["fucci_leakage_cap"]),
            washout_r2_floor=float(raw["washout_r2_floor"]),
            synergy_stability_min=float(raw["synergy_stability_min"]),
            profiles=profiles,
            tissue_params=tissue_params,
            baseline_mutation_rates=baseline_mutation_rates,
            global_noise=global_noise,
            objective=objective,
            washout_design=washout_design,
            strict_gate=strict_gate,
            sensitivity_configs=sensitivity_configs,
            science_mode=science_mode,
            science_data=science_data,
            locus_panel=locus_panel,
            mechanism_models=mechanism_models,
            inference=inference,
            claim_thresholds=claim_thresholds,
            verdict_policy=verdict_policy,
        )
    except KeyError as exc:  # pragma: no cover - sanity path
        raise ConfigError(f"Missing required config key: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid config content in {cfg_path}: {exc}") from exc
