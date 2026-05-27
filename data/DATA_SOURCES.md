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

External consistency checks map wildlife-related NTSB records to component-family cells. Dictionaries, mapping terms, sample records, bootstrap intervals, and audit summaries are under `results/experiments/transparency_checks/`.

## FAA Service Difficulty Reporting system

Source: https://av-info.faa.gov/sdrx/

Expected local folder:

- `data/raw/faa_sdr/`

The maintenance-text validation extracts wildlife-related Service Difficulty Reporting records from public records and maps component text to the same component families used for the wildlife strike review units. Summary files are under `results/experiments/jsr_validation/`.

## ASOS/METAR weather observations

Source: https://www.mesonet.agron.iastate.edu/request/download.phtml

Expected local folder:

- `data/raw/asos_metar/`

The weather validation links report airport, date, and time to hourly ASOS/METAR observations for the high-report airport set. Summary files are under `results/experiments/jsr_priority/`.

## GBIF occurrence records

Source: https://techdocs.gbif.org/en/openapi/v1/occurrence

Expected local folder:

- `data/raw/gbif/`

The ecological proxy aggregates public bird occurrence records to airport-month exposure values. Summary files are under `results/experiments/jsr_priority/`.
