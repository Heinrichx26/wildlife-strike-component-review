from __future__ import annotations

import csv
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_FILES = [
    "README.md",
    "REPRODUCIBILITY.md",
    "requirements.txt",
    "data/DATA_SOURCES.md",
    "results/rolling_review/faa_wildlife/rolling_component_review_aggregate_metrics.csv",
    "results/rolling_review/faa_wildlife/rolling_component_review_yearly_metrics.csv",
    "results/experiments/upgrade_validation/main_rolling_lift_year_resampled_ci.csv",
    "results/experiments/upgrade_validation/negative_controls_aggregate.csv",
    "results/experiments/atads_exposure/atads_exposure_aggregate.csv",
    "results/experiments/field_validation/atads_exposure_strata_aggregate.csv",
    "results/experiments/field_validation/sdr_component_enrichment.csv",
    "results/experiments/field_reliability/budget_frontier_aggregate.csv",
    "results/experiments/field_reliability/asos_weather_aggregate.csv",
    "results/experiments/field_reliability/gbif_ecological_proxy_aggregate.csv",
    "results/experiments/field_reliability/ntsb_nonwildlife_stress_check.csv",
    "results/experiments/field_reliability/ntsb_matched_stress_check.csv",
    "results/experiments/transparency_checks/ntsb_dictionary.csv",
    "results/experiments/transparency_checks/ntsb_component_mapping.csv",
    "results/experiments/transparency_checks/ntsb_stratified_audit_summary.csv",
    "results/experiments/posterior_burden/full/posterior_burden_aggregate.csv",
]

EXCLUDED_PATTERNS = [
    "." + "docx",
    "." + "pdf",
    "main" + "." + "tex",
    "supp" + "lementary",
    "Cover" + "Letter",
    "title" + "page",
    "High" + "lights",
    "manu" + "script",
]

TEXT_SUFFIXES = {".md", ".py", ".ps1", ".csv", ".txt", ".gitignore", ".bib"}
FORBIDDEN_TEXT = [
    "D:" + "\\",
    "C:" + "\\Users",
    "2025" + "data",
    "Safety" + " Science",
    "safety" + "_science",
    "JS" + "R",
    "JQ" + "ME",
    "AM" + "AR",
    "Analytic" + " Methods",
    "Journal" + " of Safety Research",
    "Journal" + " of Quality in Maintenance Engineering",
]


def fail(message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def csv_has_rows(path: Path) -> bool:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    return len(rows) >= 2


def main() -> int:
    missing = [item for item in REQUIRED_FILES if not (PROJECT_ROOT / item).is_file()]
    if missing:
        return fail("missing required files: " + ", ".join(missing))

    empty_csv = [item for item in REQUIRED_FILES if item.endswith(".csv") and not csv_has_rows(PROJECT_ROOT / item)]
    if empty_csv:
        return fail("CSV files have no data rows: " + ", ".join(empty_csv))

    excluded = []
    for path in PROJECT_ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if any(pattern.lower() in rel.lower() for pattern in EXCLUDED_PATTERNS):
                excluded.append(rel)
    if excluded:
        return fail("excluded article or submission files found: " + ", ".join(excluded))

    hits = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {".gitignore", "LICENSE"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN_TEXT:
            if pattern in text:
                hits.append(f"{path.relative_to(PROJECT_ROOT).as_posix()} contains {pattern}")
    if hits:
        return fail("; ".join(hits))

    print("Replication package check passed.")
    print(f"Checked {len(REQUIRED_FILES)} required files and excluded article/submission artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
