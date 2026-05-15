# Reproducibility notes

## Software

The analysis scripts use Python 3.10 or later. Some optional benchmarks require third-party Python packages documented by the import statements in the scripts.

## Recommended workflow

1. Download and place raw public data files according to `data/DATA_SOURCES.md`.
2. Run smoke checks before full runs.
3. Run the full analysis scripts.
4. Compare generated CSV outputs with the summary CSV files in `results/`.

## Key result files

- `results/rolling_review/faa_wildlife/rolling_component_review_aggregate_metrics.csv`
- `results/rolling_review/faa_wildlife/rolling_component_review_yearly_metrics.csv`
- `results/experiments/upgrade_validation/negative_controls_aggregate.csv`
- `results/experiments/upgrade_validation/main_rolling_lift_year_resampled_ci.csv`
- `results/experiments/atads_exposure/atads_exposure_aggregate.csv`
- `results/experiments/transparency_checks/ntsb_external_enrichment_sets.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_summary.csv`
- `results/experiments/posterior_burden/full/posterior_burden_aggregate.csv`
