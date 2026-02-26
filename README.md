Latest Simulator Result Data Here: https://drive.google.com/file/d/1HCW_FDGMg6YMfj1YDkTsWcBwtciXVBNH/view?usp=sharing

# StarAge Tranche + Science Verification Simulator

Hybrid multiscale simulation for:
1. `tranche_verdict` (operational Tranche-1 achievability gates)
2. `science_verdict` (claim-level causal evidence for claims 1-5)

## What it produces

- Monte Carlo simulation over three tissue systems (`blood_cd34`, `gut_lgr5`, `skin_stem`)
- Dose sweep for `S-Phase amplification x Cyclin throttle`
- Milestone metrics:
  - mutation-rate reduction + viability constraint
  - FUCCI S-phase gating and leakage
  - CRISPRon/CRISPRoff washout half-life estimates
  - synergistic threshold objective + stability
- Automatic tranche gate file: `tranche1_gate.json` including:
  - `pass` boolean
  - `confidence_pct`
  - threshold margins (when applicable)
  - plain-language explanation
  - `gate_definitions` (exact rules + thresholds used in the run)
- Science outputs:
  - `science_claims_verdict.json`
  - `model_comparison.json`
  - `posterior_summary.csv`
  - `data_validation_report.json`
  - `science_trace_diagnostics.json`
- Report bundles: tranche CSV/PNG outputs plus science claim/model report files

## Install

```bash
python3 -m pip install -e .
```

Optional extras:

```bash
python3 -m pip install -e .[full,dev]
```

If editable install is unavailable in a restricted environment, use the local wrapper script directly:

```bash
./starrage-sim --help
```

## CLI

Run tranche + science pipeline (+ tranche sensitivity runs if configured):

```bash
starrage-sim run --config configs/default_optimistic.yaml --out reports/run_default
```

Fit science models only:

```bash
starrage-sim fit --config configs/default_optimistic.yaml --data ./science_data --out reports/science_fit
```

Compare fitted models:

```bash
starrage-sim compare --fit reports/science_fit/science_fit.json --out reports/science_fit/model_comparison.json
```

Evaluate claims 1-5:

```bash
starrage-sim claims \
  --fit reports/science_fit/science_fit.json \
  --compare reports/science_fit/model_comparison.json \
  --out reports/science_fit/science_claims_verdict.json
```

Run single sweep profile:

```bash
starrage-sim sweep --config configs/default_optimistic.yaml --profile quick --out reports/sweep_quick
starrage-sim sweep --config configs/default_optimistic.yaml --profile full --out reports/sweep_full
```

Evaluate gate from existing metrics:

```bash
starrage-sim gate --metrics reports/run_default/metrics_summary.json --out reports/run_default/tranche1_gate.json
```

## Configuration schema

YAML keys (JSON-formatted YAML files are also supported):

- `tissues`
- `donor_ages`
- `dose_ranges`
- `washout_priors`
- `viability_threshold`
- `mutation_reduction_target` (Tranche 1 mutation gate target; now `1.80` in bundled configs)
- `gate_rule`
- `runtime_profile`
- `seed`
- `profiles`
- `tissue_params`
- `baseline_mutation_rates`
- `global_noise`
- `objective`
- `sensitivity_configs`
- `washout_design`
  - `n_timepoints`
  - `measurement_cv_scale`
  - `model_family` (`single_exponential_robust_logspace`, `bi_exponential`, `auto_single_or_bi`)
  - `holdout_fraction`
  - `retention_horizon_divisions`
  - `retention_min`
  - `predictive_metric` (`predictive_coverage` or `median_abs_log_error`)
  - `predictive_coverage_min`
  - `predictive_mae_max`
  - `gate_off_mark_required`
- `strict_gate` (`require_all_tissues`, `require_all_tissue_age`)
- `science_mode`
  - `enabled`
  - `data_mode` (`schema_first`, `data_required`, `synthetic_only`)
- `science_data`
  - `data_dir`
  - `allow_synthetic_fallback`
  - optional per-file path overrides
- `locus_panel` (`tissue`, `gene`, `locus_id`, optional coordinates)
- `mechanism_models` (`enabled_families`, default `M1-M4`)
- `inference`
  - `backend` (`pymc`)
  - `vi_settings`
  - `nuts_settings`
  - `comparison_metric` (`loo` or `waic`)
  - `sensitivity_backend` (`surrogate` default, or `advi` for lightweight Bayesian sensitivity sweeps)
- `claim_thresholds` (posterior/effect thresholds for claims 1-5)
- `verdict_policy` (`science_as_hard_blocker`)
  - `science_as_hard_blocker`
  - `allow_synthetic_science_pass_for_dev` (default `false`)

If `matplotlib` is unavailable, simulation outputs are still produced and `plots/plot_warnings.txt` is written.

## Reproducibility

- deterministic seeding per condition
- assumptions fingerprint via `assumptions_hash`
- science-layer provenance on every science JSON artifact:
  - `science_data_hash`
  - `git_commit`
  - `config_hash`
  - `seed`
  - `schema_version`
  - `preprocessing_version`
  - `preprocessing_hash`
- emitted raw-file and canonical-content traces:
  - `raw_input_hashes`
  - `canonical_content_hashes`
- machine-readable thresholds embedded in metrics output

## Interpretation

This simulator is an **achievability model under assumptions**. It is not wet-lab evidence.

Washout milestone uses a persistence + predictive gate (not a pure `R²` gate): required rows must satisfy finite CI, finite estimate fraction, retention at horizon, and holdout predictive quality. `R²` is retained as QC-only evidence.

Mutation milestone uses a configurable lower-95 target (`mutation_reduction_target`) with strict tissue/tissue-age enforcement based on `strict_gate`. Current project-wide target is `1.80x`.

Science verdict evaluates claims 1-5 against configured posterior and effect thresholds, with explicit `pass/fail/insufficient_data/synthetic_only_unvalidated` statuses.
