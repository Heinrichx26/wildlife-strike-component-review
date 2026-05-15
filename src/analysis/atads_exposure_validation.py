from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import SCORE_SPECS, score_records  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ATADS_PATH = PROJECT_ROOT / "data" / "raw" / "atads" / "atads_airport_month_ops_1995_2025_SELECTED.csv"
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "atads_exposure"
MAIN_SCORE = "component_phase_size_mass_rate"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def normalized_airport(value: object) -> str:
    code = text(value).upper()
    if len(code) == 4 and code.startswith("K"):
        return code[1:]
    return code


def load_parts() -> list[dict]:
    events = [enrich(row) for row in load_rows()]
    parts = []
    for row in component_rows(events):
        if 1995 <= int(row["year"]) <= 2025:
            item = dict(row)
            item["airport_norm"] = normalized_airport(item["airport_id"])
            parts.append(item)
    return parts


def load_atads() -> dict[tuple[str, int, int], float]:
    df = pd.read_csv(ATADS_PATH)
    lookup = {}
    for row in df.to_dict("records"):
        lookup[(str(row["locid"]).upper(), int(row["year"]), int(row["month"]))] = float(row["total_operations"])
    return lookup


def weighted_metrics(selected: list[dict], test: list[dict], target: str, weight_col: str) -> dict:
    total_y = sum(int(bool(row[target])) for row in test)
    selected_y = sum(int(bool(row[target])) for row in selected)
    overall_rate = total_y / len(test) if test else 0.0
    selected_rate = selected_y / len(selected) if selected else 0.0

    total_w = sum(float(row[weight_col]) for row in test)
    selected_w = sum(float(row[weight_col]) for row in selected)
    total_wy = sum(float(row[weight_col]) * int(bool(row[target])) for row in test)
    selected_wy = sum(float(row[weight_col]) * int(bool(row[target])) for row in selected)
    weighted_overall = total_wy / total_w if total_w else 0.0
    weighted_selected = selected_wy / selected_w if selected_w else 0.0
    return {
        "test_component_records": len(test),
        "target_records": total_y,
        "selected_component_records": len(selected),
        "captured_target_records": selected_y,
        "capture_rate": selected_y / total_y if total_y else 0.0,
        "selected_target_rate": selected_rate,
        "lift": selected_rate / overall_rate if overall_rate else 0.0,
        "selected_weighted_damage": selected_wy,
        "total_weighted_damage": total_wy,
        "weighted_capture_rate": selected_wy / total_wy if total_wy else 0.0,
        "weighted_selected_target_rate": weighted_selected,
        "weighted_overall_target_rate": weighted_overall,
        "weighted_lift": weighted_selected / weighted_overall if weighted_overall else 0.0,
    }


def evaluate() -> tuple[list[dict], list[dict]]:
    parts = load_parts()
    ops = load_atads()
    rows = []
    coverage_rows = []
    for row in parts:
        op = ops.get((row["airport_norm"], int(row["year"]), int(row["month"])))
        row["atads_operations"] = op
        row["operation_weight"] = 1.0 / math.log1p(op) if op and op > 0 else None

    for test_year in range(2000, 2026):
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test_all = [r for r in parts if int(r["year"]) == test_year]
        test = [r for r in test_all if r.get("atads_operations") and r.get("operation_weight")]
        coverage_rows.append({
            "test_year": test_year,
            "all_component_records": len(test_all),
            "matched_component_records": len(test),
            "match_rate": len(test) / len(test_all) if test_all else 0.0,
            "matched_damage_records": sum(int(bool(r["part_damage"])) for r in test),
            "matched_operations": int(sum(float(r["atads_operations"]) for r in test)),
        })
        if not train or not test:
            continue
        scored = score_records(train, test, MAIN_SCORE, "part_damage")
        for budget in [0.05, 0.10]:
            k = max(1, math.ceil(len(scored) * budget))
            selected = [row for _, row in scored[:k]]
            rows.append({
                "test_year": test_year,
                "budget_share": budget,
                "score": MAIN_SCORE,
                **weighted_metrics(selected, test, "part_damage", "operation_weight"),
            })
    return rows, coverage_rows


def aggregate(rows: list[dict]) -> list[dict]:
    out = []
    for budget in sorted({row["budget_share"] for row in rows}):
        subset = [row for row in rows if row["budget_share"] == budget]
        selected = sum(row["selected_component_records"] for row in subset)
        captured = sum(row["captured_target_records"] for row in subset)
        total = sum(row["target_records"] for row in subset)
        test = sum(row["test_component_records"] for row in subset)
        selected_rate = captured / selected
        overall_rate = total / test
        selected_weighted_damage = sum(row["selected_weighted_damage"] for row in subset)
        total_weighted_damage = sum(row["total_weighted_damage"] for row in subset)
        out.append({
            "budget_share": budget,
            "test_years": len(subset),
            "test_component_records": test,
            "target_records": total,
            "selected_component_records": selected,
            "captured_target_records": captured,
            "capture_rate": captured / total,
            "selected_target_rate": selected_rate,
            "lift": selected_rate / overall_rate,
            "weighted_capture_rate": selected_weighted_damage / total_weighted_damage if total_weighted_damage else 0.0,
            "mean_weighted_lift": sum(row["weighted_lift"] for row in subset) / len(subset),
        })
    return out


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    yearly, coverage = evaluate()
    agg = aggregate(yearly)
    write_csv(RESULT_DIR / "atads_exposure_yearly.csv", yearly)
    write_csv(RESULT_DIR / "atads_exposure_aggregate.csv", agg)
    write_csv(RESULT_DIR / "atads_match_coverage.csv", coverage)
    print(pd.DataFrame(agg).to_string(index=False))
    print(pd.DataFrame(coverage).to_string(index=False))


if __name__ == "__main__":
    main()
