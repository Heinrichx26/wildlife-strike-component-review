# Reproducibility notes

## Software

The analysis scripts use Python 3.10 or later. Third-party Python packages are listed in `requirements.txt`.

## Recommended workflow

1. Run `python src/analysis/verify_replication_package.py` to check the repository contents.
2. Download and place raw public data files according to `data/DATA_SOURCES.md`.
3. Run the data-dependent smoke checks listed in `README.md`.
4. Run the full analysis scripts.
5. Compare generated CSV outputs with the summary CSV files in `results/`.

## Key result files

- `results/rolling_review/faa_wildlife/rolling_component_review_aggregate_metrics.csv`
- `results/rolling_review/faa_wildlife/rolling_component_review_yearly_metrics.csv`
- `results/experiments/upgrade_validation/negative_controls_aggregate.csv`
- `results/experiments/upgrade_validation/main_rolling_lift_year_resampled_ci.csv`
- `results/experiments/atads_exposure/atads_exposure_aggregate.csv`
- `results/experiments/jsr_validation/atads_exposure_strata_aggregate.csv`
- `results/experiments/jsr_validation/sdr_component_enrichment.csv`
- `results/experiments/jsr_validation/sdr_component_profile.csv`
- `results/experiments/jsr_validation/weather_smoke_aggregate.csv`
- `results/experiments/jsr_priority/budget_frontier_aggregate.csv`
- `results/experiments/jsr_priority/asos_weather_aggregate.csv`
- `results/experiments/jsr_priority/gbif_ecological_proxy_aggregate.csv`
- `results/experiments/jsr_priority/ntsb_nonwildlife_stress_check.csv`
- `results/experiments/jsr_priority/reporting_bias_strata_aggregate.csv`
- `results/experiments/transparency_checks/ntsb_external_enrichment_sets.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_summary.csv`
- `results/experiments/posterior_burden/full/posterior_burden_aggregate.csv`
