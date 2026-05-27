from __future__ import annotations

import csv
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import train_scores  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SDR_DIR = PROJECT_ROOT / "data" / "raw" / "faa_sdr"
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "jsr_validation"

TEXT_FIELDS = [
    "PartName",
    "PartCondition",
    "PartLocation",
    "ComponentName",
    "ComponentLocation",
    "StageOfOperationCode",
    "Discrepancy",
]

WILDLIFE_PATTERNS = {
    "bird": r"\bbirds?\b|\bbird[- ]?strike\b",
    "wildlife": r"\bwildlife\b",
    "ingestion": r"\bingest(?:ed|ion)?\b",
    "goose_geese": r"\bgoose\b|\bgeese\b",
    "gull": r"\bgulls?\b",
    "raptor": r"\bhawk\b|\beagle\b|\bvulture\b",
    "mammal": r"\bdeer\b|\bcoyote\b|\belk\b|\bmoose\b",
}

COMPONENT_PATTERNS = {
    "engine": [
        r"\bengine\b",
        r"\bturbofan\b",
        r"\bfan blade\b",
        r"\bcompressor\b",
        r"\binlet\b",
        r"\bnacelle\b",
        r"\bingest(?:ed|ion)?\b",
    ],
    "windshield": [r"\bwindshield\b", r"\bwindscreen\b", r"\bwind screen\b"],
    "wing_rotor": [r"\bwing\b", r"\brotor\b", r"\bflap\b", r"\bslat\b", r"\baileron\b", r"\bleading edge\b"],
    "radome": [r"\bradome\b", r"\bradar dome\b"],
    "nose": [r"\bnose\b"],
    "landing_gear": [r"\blanding gear\b", r"\bgear\b", r"\bstrut\b", r"\bbrake\b"],
    "fuselage": [r"\bfuselage\b", r"\bairframe\b", r"\bskin\b"],
    "tail": [r"\btail\b", r"\bstabilizer\b", r"\brudder\b", r"\belevator\b", r"\bempennage\b"],
    "propeller": [r"\bpropeller\b", r"\bprop\b"],
    "lights": [r"\blight\b", r"\blamp\b", r"\bbeacon\b"],
}

COMPONENT_PRIORITY = [
    "engine",
    "windshield",
    "wing_rotor",
    "radome",
    "nose",
    "landing_gear",
    "fuselage",
    "tail",
    "propeller",
    "lights",
]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def normalize_blob(row: dict) -> str:
    return " ".join(str(row.get(field) or "") for field in TEXT_FIELDS).lower()


def pattern_hits(blob: str, patterns: dict[str, str]) -> list[str]:
    hits = []
    for name, pattern in patterns.items():
        if re.search(pattern, blob, re.IGNORECASE):
            hits.append(name)
    return hits


def infer_component(blob: str) -> tuple[str, str]:
    hits = []
    for component, patterns in COMPONENT_PATTERNS.items():
        if any(re.search(pattern, blob, re.IGNORECASE) for pattern in patterns):
            hits.append(component)
    for component in COMPONENT_PRIORITY:
        if component in hits:
            return component, ";".join(hits)
    return "other", ";".join(hits)


def infer_year(date_value: str, fallback_name: str) -> int:
    match = re.search(r"(20\d{2})", str(date_value or ""))
    if match:
        return int(match.group(1))
    match = re.search(r"SDR-(20\d{2})", fallback_name)
    if match:
        return int(match.group(1))
    return 0


def read_sdr_records() -> list[dict]:
    rows = []
    seen = set()
    for path in sorted(SDR_DIR.glob("SDR-20*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                blob = normalize_blob(raw)
                component, component_hits = infer_component(blob)
                wildlife_hits = pattern_hits(blob, WILDLIFE_PATTERNS)
                year = infer_year(raw.get("DifficultyDate", ""), path.name)
                key = (
                    raw.get("OperatorControlNumber", ""),
                    raw.get("DifficultyDate", ""),
                    raw.get("RegistryNNumber", ""),
                    raw.get("Discrepancy", "")[:120],
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "record_id": raw.get("OperatorControlNumber", ""),
                    "year": year,
                    "difficulty_date": raw.get("DifficultyDate", ""),
                    "aircraft_make": raw.get("AircraftMake", ""),
                    "aircraft_model": raw.get("AircraftModel", ""),
                    "stage_code": raw.get("StageOfOperationCode", ""),
                    "part_name": raw.get("PartName", ""),
                    "component_name": raw.get("ComponentName", ""),
                    "component": component,
                    "component_text_hits": component_hits,
                    "wildlife_related": int(bool(wildlife_hits)),
                    "wildlife_text_hits": ";".join(wildlife_hits),
                    "text_excerpt": text(raw.get("Discrepancy", ""))[:220],
                })
    return rows


def load_nwsd_component_risk() -> dict[str, float]:
    events = [enrich(row) for row in load_rows()]
    parts = [row for row in component_rows(events) if 2016 <= int(row["year"]) <= 2025]
    scores = train_scores(parts, ["component"], "part_damage", "rate", alpha=10.0)
    return {key[0]: value for key, value in scores.items()}


def component_reference_share(rows: list[dict], high_components: set[str], wildlife_only: bool) -> tuple[int, int]:
    population = [row for row in rows if row["component"] != "other"]
    if wildlife_only:
        population = [row for row in population if row["wildlife_related"]]
    top = sum(1 for row in population if row["component"] in high_components)
    return top, len(population) - top


def enrichment(rows: list[dict], risk: dict[str, float]) -> list[dict]:
    ranked = sorted(risk.items(), key=lambda item: (-item[1], item[0]))
    out = []
    for share in [0.20, 0.30, 0.40]:
        k = max(1, math.ceil(len(ranked) * share))
        high = {component for component, _ in ranked[:k]}
        wildlife_top, wildlife_rest = component_reference_share(rows, high, True)
        all_top, all_rest = component_reference_share(rows, high, False)
        odds_ratio = ((wildlife_top + 0.5) * (all_rest + 0.5)) / ((wildlife_rest + 0.5) * (all_top + 0.5))
        out.append({
            "high_risk_component_share": share,
            "high_risk_components": ";".join(sorted(high)),
            "mapped_sdr_records": all_top + all_rest,
            "wildlife_sdr_records": wildlife_top + wildlife_rest,
            "wildlife_records_in_high_risk_components": wildlife_top,
            "wildlife_capture_rate": wildlife_top / (wildlife_top + wildlife_rest) if wildlife_top + wildlife_rest else 0.0,
            "all_record_high_risk_share": all_top / (all_top + all_rest) if all_top + all_rest else 0.0,
            "enrichment_odds_ratio": odds_ratio,
        })
    return out


def component_profile(rows: list[dict], risk: dict[str, float]) -> list[dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"all": 0, "wildlife": 0})
    for row in rows:
        component = row["component"]
        groups[component]["all"] += 1
        groups[component]["wildlife"] += int(bool(row["wildlife_related"]))
    out = []
    for component, item in groups.items():
        out.append({
            "component": component,
            "nwsd_component_damage_score": risk.get(component, 0.0),
            "sdr_records": item["all"],
            "wildlife_sdr_records": item["wildlife"],
            "wildlife_share_within_component": item["wildlife"] / item["all"] if item["all"] else 0.0,
        })
    return sorted(out, key=lambda row: (-row["wildlife_sdr_records"], -row["nwsd_component_damage_score"]))


def dictionary_rows() -> list[dict]:
    rows = []
    for category, pattern in WILDLIFE_PATTERNS.items():
        rows.append({"dictionary": "wildlife", "category": category, "pattern": pattern})
    for component, patterns in COMPONENT_PATTERNS.items():
        for pattern in patterns:
            rows.append({"dictionary": "component", "category": component, "pattern": pattern})
    return rows


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    rows = read_sdr_records()
    risk = load_nwsd_component_risk()
    wildlife_rows = [row for row in rows if row["wildlife_related"]]
    enrichment_rows = enrichment(rows, risk)
    profile_rows = component_profile(rows, risk)
    write_csv(RESULT_DIR / "sdr_component_records_sample.csv", rows[:500])
    write_csv(RESULT_DIR / "sdr_wildlife_component_records.csv", wildlife_rows)
    write_csv(RESULT_DIR / "sdr_component_profile.csv", profile_rows)
    write_csv(RESULT_DIR / "sdr_component_enrichment.csv", enrichment_rows)
    write_csv(RESULT_DIR / "sdr_dictionary.csv", dictionary_rows())
    print(pd.DataFrame(enrichment_rows).to_string(index=False))
    print(pd.DataFrame(profile_rows).head(15).to_string(index=False))


if __name__ == "__main__":
    main()
