# Wildlife strike component reliability screening

This repository contains only replication materials for the wildlife-strike component reliability screening study: code, public-data instructions, and summary results.

## Repository structure

- `src/analysis/`: analysis scripts for rolling validation, exposure checks, external consistency checks, stress checks, permutation controls, and posterior burden extensions.
- `src/data/`: public data download helpers where automated access is available.
- `results/`: summary CSV files used to reproduce reported numeric findings.
- `data/`: data-source notes and expected local file layout. Raw public data files are not stored in this repository.

## Data

All data sources are public:

- Federal Aviation Administration National Wildlife Strike Database.
- Federal Aviation Administration Air Traffic Activity Data System airport operations.
- Federal Aviation Administration Service Difficulty Reporting system.
- National Transportation Safety Board aviation accident data system.
- ASOS/METAR public aviation weather observations.
- Global Biodiversity Information Facility occurrence records.

Large raw exports are excluded from the repository. See `data/DATA_SOURCES.md` for source links, expected folders, and download notes.

## Package smoke check

The package smoke check does not require raw data. It verifies that the repository contains the expected code, data-source instructions, audit files, and summary results, and that manuscript or submission files are not present.

```powershell
python src/analysis/verify_replication_package.py
python -m py_compile src/analysis/*.py src/data/*.py
```

## Full reproduction sequence

After placing the raw data files in the expected local folders, run the following from the repository root:

```powershell
python src/analysis/rolling_faa_wildlife_component_review.py
python src/analysis/smoke_upgrade_validation.py --full
python src/analysis/atads_exposure_validation.py
python src/analysis/field_atads_strata.py
python src/analysis/field_sdr_validation.py
python src/analysis/field_reliability_experiments.py
python src/analysis/field_asos_weather_full.py
python src/analysis/external_validation_transparency_checks.py --reps 500
python src/analysis/posterior_burden_allocation.py
```

For data-dependent smoke checks after placing the raw files:

```powershell
python src/analysis/smoke_upgrade_validation.py
python src/analysis/field_reliability_experiments.py --smoke
python src/analysis/external_validation_transparency_checks.py --smoke --reps 50
python src/analysis/posterior_burden_allocation.py --smoke
```

## Key transparency files

- `results/experiments/transparency_checks/ntsb_dictionary.csv`
- `results/experiments/transparency_checks/ntsb_component_mapping.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_records.csv`
- `results/experiments/transparency_checks/ntsb_stratified_audit_summary.csv`
- `results/experiments/transparency_checks/ntsb_external_enrichment_sets.csv`
- `results/experiments/field_reliability/ntsb_matched_stress_check.csv`

Main summary results are in `results/rolling_review/faa_wildlife/`, `results/experiments/upgrade_validation/`, `results/experiments/atads_exposure/`, `results/experiments/field_validation/`, and `results/experiments/field_reliability/`. The rolling aggregate files include the compact hierarchical rule, expanded-feature comparison rules, historical-frequency controls, and the species-size and species-size-component wildlife-domain rules.
