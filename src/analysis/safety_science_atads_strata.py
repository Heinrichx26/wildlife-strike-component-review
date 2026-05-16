from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atads_exposure_validation import load_atads, normalized_airport  # noqa: E402
from rolling_faa_wildlife_component_review import score_records  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "safety_science"
MAIN_SCORE = "component_phase_size_mass_rate"
TARGET = "part_damage"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_all_parts() -> list[dict]:
    events = [enrich(row) for row in load_rows()]
    return [row for row in component_rows(events) if 1995 <= int(row["year"]) <= 2025]


def add_operations(parts: list[dict]) -> list[dict]:
    ops = load_atads()
    out = []
    for row in parts:
        year = int(row["year"])
        if not 1995 <= year <= 2025:
            continue
        item = dict(row)
        airport = normalized_airport(item["airport_id"])
        operation_count = ops.get((airport, year, int(item["month"])))
        if not operation_count or operation_count <= 0:
            continue
        item["airport_norm"] = airport
        item["atads_operations"] = float(operation_count)
        item["operation_weight"] = 1.0 / math.log1p(float(operation_count))
        out.append(item)
    return out


def assign_operation_strata(rows: list[dict]) -> tuple[list[dict], dict]:
    values = pd.Series([row["atads_operations"] for row in rows], dtype="float64")
    low_cut = float(values.quantile(1 / 3))
    high_cut = float(values.quantile(2 / 3))
    for row in rows:
        value = float(row["atads_operations"])
        if value <= low_cut:
            row["operation_stratum"] = "low operations"
        elif value <= high_cut:
            row["operation_stratum"] = "medium operations"
        else:
            row["operation_stratum"] = "high operations"
    return rows, {"low_cut": low_cut, "high_cut": high_cut}


def metrics(selected: list[dict], population: list[dict], budget: float, score_name: str) -> dict:
    total = sum(int(bool(row[TARGET])) for row in population)
    captured = sum(int(bool(row[TARGET])) for row in selected)
    selected_weight = sum(float(row["operation_weight"]) for row in selected)
    population_weight = sum(float(row["operation_weight"]) for row in population)
    selected_weighted_damage = sum(float(row["operation_weight"]) * int(bool(row[TARGET])) for row in selected)
    population_weighted_damage = sum(float(row["operation_weight"]) * int(bool(row[TARGET])) for row in population)
    selected_rate = captured / len(selected) if selected else 0.0
    overall_rate = total / len(population) if population else 0.0
    weighted_selected_rate = selected_weighted_damage / selected_weight if selected_weight else 0.0
    weighted_overall_rate = population_weighted_damage / population_weight if population_weight else 0.0
    return {
        "score": score_name,
        "budget_share": budget,
        "test_component_records": len(population),
        "target_records": total,
        "selected_component_records": len(selected),
        "captured_target_records": captured,
        "capture_rate": captured / total if total else 0.0,
        "selected_target_rate": selected_rate,
        "overall_target_rate": overall_rate,
        "lift": selected_rate / overall_rate if overall_rate else 0.0,
        "weighted_capture_rate": selected_weighted_damage / population_weighted_damage if population_weighted_damage else 0.0,
        "weighted_lift": weighted_selected_rate / weighted_overall_rate if weighted_overall_rate else 0.0,
    }


def score_by_operations(test: list[dict]) -> list[tuple[float, dict]]:
    scored = [(float(row["atads_operations"]), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def evaluate(all_parts: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict]]:
    yearly = []
    coverage = []
    for test_year in range(2000, 2026):
        train = [row for row in all_parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in rows if int(row["year"]) == test_year]
        if not train or not test:
            continue
        scored_main = score_records(train, test, MAIN_SCORE, TARGET)
        scored_operations = score_by_operations(test)
        coverage.append({
            "test_year": test_year,
            "matched_component_records": len(test),
            "matched_damage_records": sum(int(bool(row[TARGET])) for row in test),
            "low_records": sum(1 for row in test if row["operation_stratum"] == "low operations"),
            "medium_records": sum(1 for row in test if row["operation_stratum"] == "medium operations"),
            "high_records": sum(1 for row in test if row["operation_stratum"] == "high operations"),
        })
        scored_sets = {
            "component_transition_score": scored_main,
            "airport_operations_only": scored_operations,
        }
        for score_name, scored in scored_sets.items():
            for stratum in ["all matched", "low operations", "medium operations", "high operations"]:
                if stratum == "all matched":
                    stratum_scored = scored
                else:
                    stratum_scored = [(score, row) for score, row in scored if row["operation_stratum"] == stratum]
                population = [row for _, row in stratum_scored]
                if not population:
                    continue
                for budget in [0.05, 0.10]:
                    selected_count = max(1, math.ceil(len(stratum_scored) * budget))
                    selected = [row for _, row in stratum_scored[:selected_count]]
                    yearly.append({
                        "test_year": test_year,
                        "operation_stratum": stratum,
                        **metrics(selected, population, budget, score_name),
                    })
    return yearly, coverage


def aggregate(rows: list[dict]) -> list[dict]:
    out = []
    keys = sorted({(row["score"], row["operation_stratum"], row["budget_share"]) for row in rows})
    for score_name, stratum, budget in keys:
        subset = [
            row for row in rows
            if row["score"] == score_name
            and row["operation_stratum"] == stratum
            and row["budget_share"] == budget
        ]
        selected = sum(row["selected_component_records"] for row in subset)
        captured = sum(row["captured_target_records"] for row in subset)
        target = sum(row["target_records"] for row in subset)
        total = sum(row["test_component_records"] for row in subset)
        selected_rate = captured / selected if selected else 0.0
        overall_rate = target / total if total else 0.0
        weighted_lifts = [row["weighted_lift"] for row in subset if row["weighted_lift"] > 0]
        out.append({
            "score": score_name,
            "operation_stratum": stratum,
            "budget_share": budget,
            "test_years": len({row["test_year"] for row in subset}),
            "test_component_records": total,
            "target_records": target,
            "selected_component_records": selected,
            "captured_target_records": captured,
            "capture_rate": captured / target if target else 0.0,
            "selected_target_rate": selected_rate,
            "overall_target_rate": overall_rate,
            "lift": selected_rate / overall_rate if overall_rate else 0.0,
            "mean_weighted_lift": sum(weighted_lifts) / len(weighted_lifts) if weighted_lifts else 0.0,
        })
    return out


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    all_parts = load_all_parts()
    rows, cuts = assign_operation_strata(add_operations(all_parts))
    yearly, coverage = evaluate(all_parts, rows)
    aggregate_rows = aggregate(yearly)
    write_csv(RESULT_DIR / "atads_exposure_strata_yearly.csv", yearly)
    write_csv(RESULT_DIR / "atads_exposure_strata_aggregate.csv", aggregate_rows)
    write_csv(RESULT_DIR / "atads_exposure_strata_coverage.csv", coverage)
    write_csv(RESULT_DIR / "atads_exposure_strata_cutpoints.csv", [cuts])
    print(pd.DataFrame(aggregate_rows).to_string(index=False))


if __name__ == "__main__":
    main()
