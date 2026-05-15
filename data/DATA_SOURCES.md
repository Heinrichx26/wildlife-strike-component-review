# Data sources and local layout

This repository does not store large raw public exports. Place raw files under the paths below before running the full pipeline.

## FAA National Wildlife Strike Database

Source: https://wildlife.faa.gov/home

Expected local files:

- `data/raw/faa_wildlife/faa_wildlife_export_1990.json`
- ...
- `data/raw/faa_wildlife/faa_wildlife_export_2026.json`

The study window uses 1990-2025 records, with a partial 2026 update through April 13, 2026.

## FAA ATADS airport operations

Source: https://www.faa.gov/newsroom/airport-operations-and-ranking-reports-using-air-traffic-activity-data-system-atads

Expected local file:

- `data/raw/atads/atads_airport_month_ops_1995_2025_SELECTED.csv`

The airport-month exposure check uses the queried airports represented in the ATADS match coverage result file.

## NTSB aviation accident data

Source: https://www.ntsb.gov/Pages/AviationQuery.aspx

Expected local file:

- `data/raw/ntsb_avdata/avall/avall.mdb`

External enrichment maps wildlife-related NTSB records to component-family cells. Dictionaries, mapping terms, sample records, bootstrap intervals, and audit summaries are under `results/experiments/transparency_checks/`.
