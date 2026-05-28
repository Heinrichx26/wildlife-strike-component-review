from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_faa_wildlife import enrich, load_rows  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "rolling_review" / "faa_wildlife"


SCORE_SPECS = {
    "phase_size_rate": {
        "keys": ["phase_bucket", "size"],
        "kind": "rate",
    },
    "species_phase_size_rate": {
        "keys": ["species_id", "phase_bucket", "size"],
        "kind": "rate",
    },
    "species_size_hazard_rate": {
        "keys": ["species_id", "size"],
        "kind": "rate",
    },
    "species_size_component_hazard_rate": {
        "keys": ["species_id", "size", "component"],
        "kind": "rate",
    },
    "component_only_rate": {
        "keys": ["component"],
        "kind": "rate",
    },
    "component_phase_size_rate": {
        "keys": ["component", "phase_bucket", "size"],
        "kind": "rate",
    },
    "component_phase_size_mass_rate": {
        "keys": ["component", "phase_bucket", "size", "aircraft_mass_class"],
        "kind": "rate",
    },
    "component_phase_size_mass_frequency": {
        "keys": ["component", "phase_bucket", "size", "aircraft_mass_class"],
        "kind": "frequency",
    },
}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def key_for(row: dict, keys: list[str]) -> tuple:
    return tuple(row.get(k, "") for k in keys)


def train_scores(train: list[dict], keys: list[str], target: str, kind: str, alpha: float = 10.0) -> dict[tuple, float]:
    stats: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "y": 0})
    for row in train:
        item = stats[key_for(row, keys)]
        item["n"] += 1
        item["y"] += int(bool(row[target]))
    total_n = sum(v["n"] for v in stats.values())
    total_y = sum(v["y"] for v in stats.values())
    prior = total_y / total_n if total_n else 0.0
    scores = {}
    for key, item in stats.items():
        if kind == "frequency":
            scores[key] = float(item["n"])
        else:
            scores[key] = (item["y"] + alpha * prior) / (item["n"] + alpha)
    return scores


def score_records(train: list[dict], test: list[dict], spec_name: str, target: str) -> list[tuple[float, dict]]:
    spec = SCORE_SPECS[spec_name]
    keys = spec["keys"]
    scores = train_scores(train, keys, target, spec["kind"])
    scored = []
    for row in test:
        score = scores.get(key_for(row, keys), 0.0)
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def evaluate_year(
    parts: list[dict],
    test_year: int,
    train_window: int,
    budgets: list[float],
    targets: list[str],
) -> tuple[list[dict], dict[tuple[str, str, float], set[tuple[str, str]]]]:
    train_years = set(range(test_year - train_window, test_year))
    train = [r for r in parts if r["year"] in train_years]
    test = [r for r in parts if r["year"] == test_year]
    metrics = []
    selected_sets: dict[tuple[str, str, float], set[tuple[str, str]]] = {}
    if not train or not test:
        return metrics, selected_sets

    for target in targets:
        total_target = sum(int(bool(r[target])) for r in test)
        if total_target == 0:
            continue
        overall_rate = total_target / len(test)
        for spec_name in SCORE_SPECS:
            scored = score_records(train, test, spec_name, target)
            for budget in budgets:
                selected_count = max(1, math.ceil(len(scored) * budget))
                selected = [row for _, row in scored[:selected_count]]
                captured = sum(int(bool(row[target])) for row in selected)
                selected_rate = captured / selected_count
                selected_key = (target, spec_name, budget)
                selected_sets[selected_key] = {(row["event_id"], row["component"]) for row in selected}
                metrics.append({
                    "test_year": test_year,
                    "train_start": test_year - train_window,
                    "train_end": test_year - 1,
                    "train_window": train_window,
                    "target": target,
                    "score": spec_name,
                    "budget_share": budget,
                    "test_component_records": len(test),
                    "target_records": total_target,
                    "selected_component_records": selected_count,
                    "captured_target_records": captured,
                    "capture_rate": captured / total_target,
                    "selected_target_rate": selected_rate,
                    "overall_target_rate": overall_rate,
                    "lift": selected_rate / overall_rate if overall_rate else 0.0,
                })
    return metrics, selected_sets


def aggregate_metrics(yearly: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "test_years": [],
        "test_component_records": 0,
        "target_records": 0,
        "selected_component_records": 0,
        "captured_target_records": 0,
        "annual_capture_rates": [],
        "annual_lifts": [],
    })
    for row in yearly:
        key = (row["target"], row["score"], row["budget_share"], row["train_window"])
        item = groups[key]
        item["test_years"].append(row["test_year"])
        item["test_component_records"] += row["test_component_records"]
        item["target_records"] += row["target_records"]
        item["selected_component_records"] += row["selected_component_records"]
        item["captured_target_records"] += row["captured_target_records"]
        item["annual_capture_rates"].append(row["capture_rate"])
        item["annual_lifts"].append(row["lift"])

    out = []
    for key, item in groups.items():
        target, score, budget, train_window = key
        selected_rate = item["captured_target_records"] / item["selected_component_records"]
        overall_rate = item["target_records"] / item["test_component_records"]
        out.append({
            "target": target,
            "score": score,
            "budget_share": budget,
            "train_window": train_window,
            "first_test_year": min(item["test_years"]),
            "last_test_year": max(item["test_years"]),
            "num_test_years": len(set(item["test_years"])),
            "test_component_records": item["test_component_records"],
            "target_records": item["target_records"],
            "selected_component_records": item["selected_component_records"],
            "captured_target_records": item["captured_target_records"],
            "pooled_capture_rate": item["captured_target_records"] / item["target_records"],
            "pooled_selected_target_rate": selected_rate,
            "pooled_overall_target_rate": overall_rate,
            "pooled_lift": selected_rate / overall_rate if overall_rate else 0.0,
            "mean_annual_capture_rate": sum(item["annual_capture_rates"]) / len(item["annual_capture_rates"]),
            "mean_annual_lift": sum(item["annual_lifts"]) / len(item["annual_lifts"]),
        })
    return sorted(out, key=lambda x: (x["target"], x["budget_share"], -x["pooled_lift"]))


def selected_records_for(parts: list[dict], test_year: int, train_window: int, target: str, spec_name: str, budget: float) -> list[dict]:
    train_years = set(range(test_year - train_window, test_year))
    train = [r for r in parts if r["year"] in train_years]
    test = [r for r in parts if r["year"] == test_year]
    scored = score_records(train, test, spec_name, target)
    selected_count = max(1, math.ceil(len(scored) * budget))
    return [row for _, row in scored[:selected_count]]


def counterfactual_added_records(parts: list[dict], train_window: int, budget: float) -> list[dict]:
    out = []
    for test_year in range(1995, 2026):
        risk = selected_records_for(parts, test_year, train_window, "part_damage", "component_phase_size_mass_rate", budget)
        freq = selected_records_for(parts, test_year, train_window, "part_damage", "component_phase_size_mass_frequency", budget)
        freq_keys = {(row["event_id"], row["component"]) for row in freq}
        added = [row for row in risk if (row["event_id"], row["component"]) not in freq_keys]
        for row in added:
            if row["part_damage"] or row["event_hard"] or row["cost"] > 0 or row["aos"] > 0:
                out.append({
                    "test_year": test_year,
                    "event_id": row["event_id"],
                    "component": row["component"],
                    "phase_bucket": row["phase_bucket"],
                    "size": row["size"],
                    "aircraft_mass_class": row["aircraft_mass_class"],
                    "part_damage": row["part_damage"],
                    "event_hard": row["event_hard"],
                    "cost": round(row["cost"], 2),
                    "aos": round(row["aos"], 2),
                    "species": row["species"],
                    "airport_id": row["airport_id"],
                    "airport": row["airport"],
                    "aircraft": row["aircraft"],
                })
    return sorted(out, key=lambda x: (-int(x["part_damage"]), -x["cost"], -x["aos"], x["test_year"]))[:200]


def summarize_counterfactual(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {"records": 0, "part_damage": 0, "hard": 0, "cost": 0.0, "aos": 0.0})
    for row in rows:
        key = (row["component"], row["phase_bucket"], row["size"], row["aircraft_mass_class"])
        item = groups[key]
        item["records"] += 1
        item["part_damage"] += int(bool(row["part_damage"]))
        item["hard"] += int(bool(row["event_hard"]))
        item["cost"] += row["cost"]
        item["aos"] += row["aos"]
    out = []
    for key, item in groups.items():
        out.append({
            "component": key[0],
            "phase_bucket": key[1],
            "size": key[2],
            "aircraft_mass_class": key[3],
            "added_records": item["records"],
            "part_damage_records": item["part_damage"],
            "hard_event_records": item["hard"],
            "cost": round(item["cost"], 2),
            "aos": round(item["aos"], 2),
        })
    return sorted(out, key=lambda x: (-x["part_damage_records"], -x["cost"], -x["aos"]))[:50]


def fmt_pct(value: float) -> str:
    return f"{value:.1%}"


def build_report(aggregate: list[dict], counter_summary: list[dict], added_examples: list[dict]) -> str:
    chosen = [
        row for row in aggregate
        if row["budget_share"] in {0.05, 0.10}
        and row["target"] in {"part_damage", "event_hard"}
    ]
    lines = [
        "# FAA wildlife strike component review rolling validation",
        "",
        "## Inspection setting",
        "",
        "- Data window: 1990-2025 records are used for the main rolling validation.",
        "- Validation design: the previous five years train each next-year selected set, with test years 1995-2025.",
        "- Inspection capacities: the ranked list selects the highest-risk 5% or 10% of component-family units.",
        "- Primary target: part_damage indicates observed same-report component damage.",
        "- Supporting target: event_hard indicates event-level damage, cost, downtime, flight effect, injury, or fatality.",
        "",
        "## Rolling validation summary",
        "",
        "| Target | Ranking rule | Budget | Captured | Capture | Selected hit rate | Overall hit rate | Lift |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    order = {
        "component_phase_size_mass_rate": 0,
        "component_phase_size_rate": 1,
        "phase_size_rate": 2,
        "species_phase_size_rate": 3,
        "component_phase_size_mass_frequency": 4,
        "component_only_rate": 5,
    }
    for row in sorted(chosen, key=lambda x: (x["target"], x["budget_share"], order.get(x["score"], 99))):
        lines.append(
            f"| {row['target']} | {row['score']} | {row['budget_share']:.0%} | "
            f"{row['captured_target_records']:,}/{row['target_records']:,} | {fmt_pct(row['pooled_capture_rate'])} | "
            f"{fmt_pct(row['pooled_selected_target_rate'])} | {fmt_pct(row['pooled_overall_target_rate'])} | "
            f"{row['pooled_lift']:.2f} |"
        )

    lines.extend([
        "",
        "## Counterfactual review results",
        "",
        "The table summarizes component-family units selected by the transition ranking and missed by the historical-frequency ranking under the same inspection capacity.",
        "",
        "| Component | Phase | Size | Aircraft mass class | Added records | Component damage | Event consequence | Cost | AOS hours |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in counter_summary[:15]:
        lines.append(
            f"| {row['component']} | {row['phase_bucket']} | {row['size']} | {row['aircraft_mass_class']} | "
            f"{row['added_records']:,} | {row['part_damage_records']:,} | {row['hard_event_records']:,} | "
            f"{row['cost']:,.0f} | {row['aos']:,.1f} |"
        )

    lines.extend([
        "",
        "## High-cost counterfactual examples",
        "",
        "| Year | Airport | Aircraft | Component | Phase | Size | Species | Component damage | Cost | AOS hours |",
        "|---:|---|---|---|---|---|---|---:|---:|---:|",
    ])
    for row in added_examples[:10]:
        lines.append(
            f"| {row['test_year']} | {row['airport_id']} | {row['aircraft']} | {row['component']} | "
            f"{row['phase_bucket']} | {row['size']} | {str(row['species']).replace('|', '/')} | "
            f"{int(bool(row['part_damage']))} | {row['cost']:,.0f} | {row['aos']:,.1f} |"
        )

    lines.extend([
        "",
        "## Result interpretation",
        "",
        "- Component transition ranking is evaluated against fixed-capacity alternatives across rolling future years.",
        "- Counterfactual sets summarize damage, cost, and downtime missed by frequency-based review under the same capacity.",
        "- Rolling validation keeps training and test years separated before selected sets are evaluated.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    event_rows = [enrich(row) for row in load_rows()]
    event_rows = [r for r in event_rows if 1990 <= r["_YEAR"] <= 2025]
    parts = component_rows(event_rows)

    budgets = [0.01, 0.05, 0.10, 0.20]
    targets = ["part_damage", "event_hard"]
    train_window = 5
    yearly = []
    for test_year in range(1995, 2026):
        metrics, _ = evaluate_year(parts, test_year, train_window, budgets, targets)
        yearly.extend(metrics)

    aggregate = aggregate_metrics(yearly)
    added = counterfactual_added_records(parts, train_window, 0.05)
    counter_summary = summarize_counterfactual(added)

    write_csv(RESULT_DIR / "rolling_component_review_yearly_metrics.csv", yearly)
    write_csv(RESULT_DIR / "rolling_component_review_aggregate_metrics.csv", aggregate)
    write_csv(RESULT_DIR / "rolling_component_review_counterfactual_added_records.csv", added)
    write_csv(RESULT_DIR / "rolling_component_review_counterfactual_summary.csv", counter_summary)

    report = build_report(aggregate, counter_summary, added)
    (RESULT_DIR / "rolling_component_review_report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
