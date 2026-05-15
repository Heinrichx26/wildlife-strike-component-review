from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import selected_records_for  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "rolling_review" / "faa_wildlife"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def full_added_records(parts: list[dict], train_window: int = 5, budget: float = 0.05) -> list[dict]:
    out = []
    for test_year in range(1995, 2026):
        risk = selected_records_for(
            parts,
            test_year,
            train_window,
            "part_damage",
            "component_phase_size_mass_rate",
            budget,
        )
        freq = selected_records_for(
            parts,
            test_year,
            train_window,
            "part_damage",
            "component_phase_size_mass_frequency",
            budget,
        )
        freq_keys = {(row["event_id"], row["component"]) for row in freq}
        for row in risk:
            if (row["event_id"], row["component"]) in freq_keys:
                continue
            if not (row["part_damage"] or row["event_hard"] or row["cost"] > 0 or row["aos"] > 0):
                continue
            out.append({
                "test_year": test_year,
                "event_id": row["event_id"],
                "component": row["component"],
                "phase_bucket": row["phase_bucket"],
                "size": row["size"],
                "aircraft_mass_class": row["aircraft_mass_class"],
                "part_damage": int(bool(row["part_damage"])),
                "event_hard": int(bool(row["event_hard"])),
                "cost": round(float(row["cost"]), 2),
                "aos": round(float(row["aos"]), 2),
            })
    return out


def combo_summary(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["component"], row["phase_bucket"], row["size"], row["aircraft_mass_class"])
        groups[key].append(row)

    out = []
    for key, items in groups.items():
        event_rows = {}
        for item in items:
            event_rows.setdefault(item["event_id"], item)
        unique_items = list(event_rows.values())
        out.append({
            "component": key[0],
            "phase_bucket": key[1],
            "size": key[2],
            "aircraft_mass_class": key[3],
            "added_component_units": len(items),
            "unique_events": len(unique_items),
            "component_damage_units": sum(int(item["part_damage"]) for item in items),
            "unit_level_event_consequence": sum(int(item["event_hard"]) for item in items),
            "unique_event_consequence": sum(int(item["event_hard"]) for item in unique_items),
            "associated_event_cost": round(sum(float(item["cost"]) for item in items), 2),
            "event_dedup_cost_within_combo": round(sum(float(item["cost"]) for item in unique_items), 2),
            "associated_event_aos": round(sum(float(item["aos"]) for item in items), 2),
            "event_dedup_aos_within_combo": round(sum(float(item["aos"]) for item in unique_items), 2),
        })
    return sorted(
        out,
        key=lambda x: (-x["component_damage_units"], -x["associated_event_cost"], -x["associated_event_aos"]),
    )


def accounting_summary(rows: list[dict]) -> list[dict]:
    event_rows = {}
    for row in rows:
        event_rows.setdefault(row["event_id"], row)
    unique_items = list(event_rows.values())
    return [
        {
            "accounting": "component-unit-associated",
            "component_units": len(rows),
            "unique_events": len(event_rows),
            "component_damage_units": sum(int(row["part_damage"]) for row in rows),
            "event_consequence_records": sum(int(row["event_hard"]) for row in rows),
            "cost": round(sum(float(row["cost"]) for row in rows), 2),
            "aos": round(sum(float(row["aos"]) for row in rows), 2),
        },
        {
            "accounting": "event-deduplicated",
            "component_units": len(rows),
            "unique_events": len(event_rows),
            "component_damage_units": sum(int(row["part_damage"]) for row in rows),
            "event_consequence_records": sum(int(row["event_hard"]) for row in unique_items),
            "cost": round(sum(float(row["cost"]) for row in unique_items), 2),
            "aos": round(sum(float(row["aos"]) for row in unique_items), 2),
        },
    ]


def main() -> None:
    event_rows = [enrich(row) for row in load_rows()]
    event_rows = [row for row in event_rows if 1990 <= row["_YEAR"] <= 2025]
    parts = component_rows(event_rows)
    added = full_added_records(parts)
    write_csv(RESULT_DIR / "rolling_component_review_counterfactual_full_added_records.csv", added)
    write_csv(RESULT_DIR / "rolling_component_review_counterfactual_full_combo_summary.csv", combo_summary(added))
    write_csv(RESULT_DIR / "rolling_component_review_counterfactual_event_accounting.csv", accounting_summary(added))
    print(f"Full counterfactual added component units with outcomes: {len(added):,}")


if __name__ == "__main__":
    main()
