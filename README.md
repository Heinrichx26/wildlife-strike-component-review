# Wildlife strike component review allocation

This repository contains only replication materials for the wildlife-strike component review allocation study: code, public-data instructions, and summary results.

## Repository structure

- `src/analysis/`: analysis scripts for rolling validation, exposure checks, external enrichment checks, negative controls, and posterior burden extensions.
- `src/data/`: public data download helpers where automated access is available.
- `results/`: summary CSV files used to reproduce reported numeric findings.
- `data/`: data-source notes and expected local file layout. Raw public data files are not stored in this repository.

## Data

All data sources are public:

- Federal Aviation Administration National Wildlife Strike Database.
- Federal Aviation Administration Air Traffic Activity Data System airport operations.
- Federal Aviation Administration Service Difficulty Reporting system.
- National Transportation Safety Board aviation accident data system.
- National Oceanic and Atmospheric Administration Global Hourly weather archive.

Large raw exports are excluded from the repository. See `data/DATA_SOURCES.md` for source links, expected folders, and download notes.

## Minimal reproduction sequence

After placing the raw data files in the expected local folders, run the following from the repository root:

```powershell
python src/analysis/rolling_faa_wildlife_component_review.py
python src/analysis/smoke_upgrade_validation.py --full
python src/analysis/atads_exposure_validation.py
python src/analysis/safety_science_atads_strata.py
python src/analysis/safety_science_sdr_validation.py
python src/analysis/safety_science_weather_smoke.py
python src/analysis/external_validation_transparency_checks.py --reps 500
python src/analysis/posterior_burden_allocation.py
```

For a quick pre-check:

```powershell
python src/analysis/smoke_upgrade_validation.py
python src/analysis/safety_science_weather_smoke.py --smoke
python src/analysis/external_validation_transparency_checks.py --smoke --reps 50
python src/analysis/posterior_burden_allocation.py --smoke
```

## Key transparency files

- `results/experiments/transparency_checks/ntsb_dictionary.csv`
- `results/experiments/transparency_checks/ntsb_component_mapping.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_records.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_summary.csv`
- `results/experiments/transparency_checks/ntsb_external_enrichment_sets.csv`

Main summary results are in `results/rolling_review/faa_wildlife/`, `results/experiments/upgrade_validation/`, `results/experiments/atads_exposure/`, and `results/experiments/safety_science/`.
