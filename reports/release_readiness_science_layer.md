# Release Readiness Checklist: Tier-3 Science Layer

Date: 2026-02-26
Repository: /Users/sebastian/StarAge

## Pre-tag release gate
- [ ] Full suite passes in CI on supported OS/Python matrix.
- [ ] Fresh install smoke test passes: `pip install .[science]`, `starrage-sim --help`, `starrage-sim run --config configs/ci_science_smoke.json --out ...`.
- [ ] `SCIENCE_SCHEMA_VERSION` reviewed and bumped if schemas changed.
- [ ] Golden artifacts updated intentionally with changelog note.
- [ ] `verdict_overview.md` states: Tranche Gate != Science Verdict != biological proof.
- [ ] Release notes include additive outputs, breaking changes (if any), and known risks.

## Current status
- Status: READY_FOR_CI_VALIDATION.
- Last full local baseline before this hardening pass: `22 passed in 438.06s (0:07:18)`.
- This pass intentionally did not rerun tests per instruction; CI is now configured to provide release-grade matrix evidence.
- Fresh clean-install smoke passed locally in isolated venv (`pip install .[science]`, `starrage-sim --help`, `starrage-sim run --config configs/ci_science_smoke.json --out /tmp/starrage-clean-smoke-run`).

## What is now hardened

### 1) Provenance hash stability and canonicalization
- `science_data_hash` now includes canonicalized dataset content (not just row counts), schema identity, order-independent locus panel, preprocessing descriptor, and preprocessing hash.
- Canonicalization rules now explicitly enforce:
  - deterministic row sorting by required schema columns,
  - stable float formatting (12-decimal canonical tokenization for hash inputs),
  - NA normalization (`None`, NaN-ish tokens -> `__NA__`),
  - schema signatures (column name + dtype + unit/scale placeholders).
- Raw file-level identity remains separately emitted in `raw_input_hashes` to preserve exact reproduction traces.

### 2) Golden tests tuned to reduce false-red drift
- Science JSON writing now normalizes numeric payloads at serialization boundaries (`canonicalize_json_payload`, float rounding, non-finite -> null).
- Golden comparison test now performs strict structural comparison with numeric tolerance for numeric leaves, reducing churn from tiny floating-point differences while keeping key/list structure strict.

### 3) CI matrix and reproducibility workflow
- Added workflow: `.github/workflows/release-readiness.yml`.
- Matrix coverage configured:
  - OS: Ubuntu + macOS
  - Python: 3.11 + 3.12
- Clean-install smoke job included:
  - fresh `pip install .[science]`
  - CLI smoke (`--help` + `run` with `configs/ci_science_smoke.json`).

### 4) Performance envelope documented and codified
- Test calibration mode is now explicit:
  - default: `STARRAGE_TEST_MODE=fast`
  - optional deep mode: `STARRAGE_TEST_MODE=deep`
- Fast/deep sampling parameters are encoded in `tests/science_utils.py`.
- CI jobs include explicit timeout budgets (`35m` matrix, `20m` smoke) so regressions fail intentionally.

### 5) Backward compatibility and versioning posture
- Compatibility statement:
  - Tranche artifacts remain unchanged for existing consumers.
  - Science artifacts are additive and provenance-enriched.
- Schema/version policy:
  - `SCIENCE_SCHEMA_VERSION` is the contract bump point for schema-level changes.
- Package SemVer plan for this change set:
  - target next minor bump (`0.1.x` -> `0.2.0`) for additive science-surface expansion.

## Files touched for this hardening pass
- `src/starrage_sim/data/loaders.py`
- `src/starrage_sim/provenance.py`
- `src/starrage_sim/engine/science_pipeline.py`
- `src/starrage_sim/cli/main.py`
- `tests/test_science_golden_artifacts.py`
- `tests/test_science_data_modes.py`
- `tests/science_utils.py`
- `configs/ci_science_smoke.json`
- `.github/workflows/release-readiness.yml`

## Downstream consumer migration note
- No mandatory migration for tranche-only consumers.
- Science consumers should treat provenance fields as required for reproducibility workflows:
  - `science_data_hash`, `git_commit`, `config_hash`, `seed`, `schema_version`, `preprocessing_version`, `preprocessing_hash`.
- Golden snapshots are strict by design; update only with intentional behavior changes and changelog entry.

## Reproduction commands
```bash
# Local full test (when running locally)
PYTHONPATH=src ./.venv/bin/python -m pytest -ra --durations=25 --durations-min=0.1

# Local science-focused subset
PYTHONPATH=src ./.venv/bin/python -m pytest -q \
  tests/test_science_data_modes.py \
  tests/test_science_calibration.py \
  tests/test_science_golden_artifacts.py \
  tests/test_science_run_integration.py
```
