from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import key_for, score_records  # noqa: E402
from smoke_upgrade_validation import (  # noqa: E402
    HIERARCHY_LEVELS,
    FREQ_SCORE,
    HIER_SCORE,
    MAIN_SCORE,
    hierarchical_score_for,
    load_parts,
    score_hierarchical,
    train_hierarchical_score_tables,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "posterior_burden"

TARGET = "part_damage"
KEYS = ["component", "phase_bucket", "size", "aircraft_mass_class"]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def component_flags(row: dict) -> list[float]:
    component = row.get("component", "")
    phase = row.get("phase_bucket", "")
    size = row.get("size", "")
    mass = row.get("aircraft_mass_class", "")
    return [
        float(component == "engine"),
        float(component in {"wing_rotor", "propeller"}),
        float(component in {"windshield", "nose", "radome"}),
        float(component == "landing_gear"),
        float(phase == "departure"),
        float(phase == "arrival"),
        float(phase == "enroute"),
        float(size == "LARGE"),
        float(size == "MEDIUM"),
        float(mass == "4"),
        float(mass == "5"),
    ]


def train_hierarchical_mean_tables(train: list[dict], values: dict[tuple[str, str], float], alpha: float = 10.0) -> tuple[dict[tuple, dict[tuple, float]], float]:
    global_n = len(train)
    global_sum = sum(values[(row["event_id"], row["component"])] for row in train)
    global_mean = global_sum / global_n if global_n else 0.0
    level_scores: dict[tuple, dict[tuple, float]] = {}
    parent_scores: dict[tuple, float] = {(): global_mean}
    for level in HIERARCHY_LEVELS:
        stats: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "sum": 0.0})
        for row in train:
            k = tuple(row.get(col, "") for col in level)
            stats[k]["n"] += 1
            stats[k]["sum"] += values[(row["event_id"], row["component"])]
        scores = {}
        for key, item in stats.items():
            parent_key = key[:-1] if len(key) > 1 else ()
            prior = parent_scores.get(parent_key, global_mean)
            scores[key] = (item["sum"] + alpha * prior) / (item["n"] + alpha)
        level_scores[tuple(level)] = scores
        parent_scores = scores
    return level_scores, global_mean


def hierarchical_mean_for(row: dict, tables: dict[tuple, dict[tuple, float]], global_mean: float) -> float:
    score = global_mean
    for level in HIERARCHY_LEVELS:
        key = tuple(row.get(col, "") for col in level)
        score = tables.get(tuple(level), {}).get(key, score)
    return score


def burden_values(train: list[dict]) -> tuple[dict[tuple[str, str], float], dict]:
    log_costs = [math.log1p(float(row["cost"])) for row in train if float(row["cost"]) > 0]
    log_aos = [math.log1p(float(row["aos"])) for row in train if float(row["aos"]) > 0]
    cost_scale = max(1.0, quantile(log_costs, 0.95))
    aos_scale = max(1.0, quantile(log_aos, 0.95))
    values = {}
    for row in train:
        unit_key = (row["event_id"], row["component"])
        values[unit_key] = (
            float(bool(row["part_damage"]))
            + float(bool(row["event_hard"]))
            + safe_div(math.log1p(float(row["cost"])), cost_scale)
            + safe_div(math.log1p(float(row["aos"])), aos_scale)
        )
    return values, {"cost_scale": cost_scale, "aos_scale": aos_scale}


def train_binary_copy(train: list[dict], field: str, values: dict[tuple[str, str], bool]) -> list[dict]:
    out = []
    for row in train:
        item = dict(row)
        item[field] = bool(values[(row["event_id"], row["component"])])
        out.append(item)
    return out


def cell_stats(train: list[dict], p_tables: dict, p_global: float, h_tables: dict, h_global: float, scales: dict) -> dict[tuple, dict]:
    stats: dict[tuple, dict] = defaultdict(lambda: {
        "n": 0,
        "damage": 0,
        "hard": 0,
        "log_cost": 0.0,
        "log_aos": 0.0,
        "example": None,
    })
    for row in train:
        k = key_for(row, KEYS)
        item = stats[k]
        item["n"] += 1
        item["damage"] += int(bool(row["part_damage"]))
        item["hard"] += int(bool(row["event_hard"]))
        item["log_cost"] += safe_div(math.log1p(float(row["cost"])), scales["cost_scale"])
        item["log_aos"] += safe_div(math.log1p(float(row["aos"])), scales["aos_scale"])
        item["example"] = row
    out = {}
    for k, item in stats.items():
        row = item["example"]
        out[k] = {
            "n": item["n"],
            "features": [
                hierarchical_score_for(row, p_tables, p_global),
                hierarchical_score_for(row, h_tables, h_global),
                item["log_cost"] / item["n"],
                item["log_aos"] / item["n"],
                *component_flags(row),
            ],
        }
    return out


def standardize_features(items: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    if not items:
        return [], [], []
    width = len(items[0])
    means = [sum(row[j] for row in items) / len(items) for j in range(width)]
    sds = []
    for j in range(width):
        var = sum((row[j] - means[j]) ** 2 for row in items) / max(1, len(items) - 1)
        sds.append(math.sqrt(var) if var > 1e-12 else 1.0)
    z = [[(row[j] - means[j]) / sds[j] for j in range(width)] for row in items]
    return z, means, sds


def kmeans(features: list[list[float]], k: int = 4, iterations: int = 30) -> tuple[list[int], list[list[float]]]:
    if not features:
        return [], []
    k = min(k, len(features))
    seeds = [features[int(i * (len(features) - 1) / max(1, k - 1))] for i in range(k)]
    centers = [list(seed) for seed in seeds]
    labels = [0] * len(features)
    for _ in range(iterations):
        changed = False
        for i, row in enumerate(features):
            distances = [sum((row[j] - center[j]) ** 2 for j in range(len(row))) for center in centers]
            label = min(range(k), key=lambda idx: distances[idx])
            if label != labels[i]:
                labels[i] = label
                changed = True
        sums = [[0.0] * len(features[0]) for _ in range(k)]
        counts = [0] * k
        for label, row in zip(labels, features):
            counts[label] += 1
            for j, value in enumerate(row):
                sums[label][j] += value
        for idx in range(k):
            if counts[idx]:
                centers[idx] = [value / counts[idx] for value in sums[idx]]
        if not changed:
            break
    return labels, centers


def fit_tail_expectation(values: list[float]) -> float:
    positives = [value for value in values if value > 0]
    if not positives:
        return 0.0
    threshold = quantile(positives, 0.90)
    exceed = [value - threshold for value in positives if value > threshold]
    if len(exceed) < 10:
        return sum(v for v in positives if v >= threshold) / max(1, sum(1 for v in positives if v >= threshold))
    mean_excess = sum(exceed) / len(exceed)
    variance = sum((value - mean_excess) ** 2 for value in exceed) / max(1, len(exceed) - 1)
    if variance <= 1e-12:
        return threshold + mean_excess
    ratio = (mean_excess * mean_excess) / variance
    xi = max(-0.5, min(0.8, 0.5 * (1.0 - ratio)))
    beta = max(1e-9, 0.5 * mean_excess * (1.0 + ratio))
    return threshold + beta / (1.0 - xi)


def train_latent_tail(train: list[dict], p_tables: dict, p_global: float, h_tables: dict, h_global: float, scales: dict) -> dict:
    stats = cell_stats(train, p_tables, p_global, h_tables, h_global, scales)
    keys = sorted(stats)
    raw_features = [stats[k]["features"] for k in keys]
    z_features, means, sds = standardize_features(raw_features)
    labels, centers = kmeans(z_features, k=4)
    cell_regime = {k: label for k, label in zip(keys, labels)}
    regime_values: dict[int, list[float]] = defaultdict(list)
    global_values = []
    for row in train:
        k = key_for(row, KEYS)
        label = cell_regime.get(k, 0)
        value = safe_div(math.log1p(float(row["cost"])), scales["cost_scale"]) + safe_div(math.log1p(float(row["aos"])), scales["aos_scale"])
        regime_values[label].append(value)
        global_values.append(value)
    global_tail = fit_tail_expectation(global_values)
    regime_tail = {label: fit_tail_expectation(values) or global_tail for label, values in regime_values.items()}
    return {
        "cell_regime": cell_regime,
        "centers": centers,
        "means": means,
        "sds": sds,
        "regime_tail": regime_tail,
        "global_tail": global_tail,
    }


def assign_regime(row: dict, latent: dict, p_damage: float, p_hard: float, expected_burden: float) -> int:
    k = key_for(row, KEYS)
    if k in latent["cell_regime"]:
        return latent["cell_regime"][k]
    raw = [p_damage, p_hard, expected_burden, 0.0, *component_flags(row)]
    z = [(raw[j] - latent["means"][j]) / latent["sds"][j] for j in range(len(raw))]
    distances = [sum((z[j] - center[j]) ** 2 for j in range(len(z))) for center in latent["centers"]]
    return min(range(len(distances)), key=lambda idx: distances[idx]) if distances else 0


def train_scoring_context(train: list[dict]) -> dict:
    p_tables, p_global = train_hierarchical_score_tables(train, "part_damage")
    h_tables, h_global = train_hierarchical_score_tables(train, "event_hard")
    values, scales = burden_values(train)
    burden_tables, burden_global = train_hierarchical_mean_tables(train, values)
    log_costs = [math.log1p(float(row["cost"])) for row in train if float(row["cost"]) > 0]
    log_aos = [math.log1p(float(row["aos"])) for row in train if float(row["aos"]) > 0]
    cost_tail = quantile(log_costs, 0.90)
    aos_tail = quantile(log_aos, 0.90)
    tail_flags = {
        (row["event_id"], row["component"]): (
            (math.log1p(float(row["cost"])) >= cost_tail and float(row["cost"]) > 0)
            or (math.log1p(float(row["aos"])) >= aos_tail and float(row["aos"]) > 0)
        )
        for row in train
    }
    tail_train = train_binary_copy(train, "tail_event", tail_flags)
    tail_tables, tail_global = train_hierarchical_score_tables(tail_train, "tail_event")
    latent = train_latent_tail(train, p_tables, p_global, h_tables, h_global, scales)
    return {
        "p_tables": p_tables,
        "p_global": p_global,
        "h_tables": h_tables,
        "h_global": h_global,
        "burden_tables": burden_tables,
        "burden_global": burden_global,
        "tail_tables": tail_tables,
        "tail_global": tail_global,
        "latent": latent,
    }


def score_posterior_burden(train: list[dict], test: list[dict]) -> dict[str, list[tuple[float, dict]]]:
    context = train_scoring_context(train)
    variants: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for row in test:
        p_damage = hierarchical_score_for(row, context["p_tables"], context["p_global"])
        p_hard = hierarchical_score_for(row, context["h_tables"], context["h_global"])
        expected_burden = hierarchical_mean_for(row, context["burden_tables"], context["burden_global"])
        p_tail = hierarchical_score_for(row, context["tail_tables"], context["tail_global"])
        regime = assign_regime(row, context["latent"], p_damage, p_hard, expected_burden)
        tail_expected = context["latent"]["regime_tail"].get(regime, context["latent"]["global_tail"])
        variants["posterior_expected_burden"].append((expected_burden, row))
        variants["posterior_damage_gated_burden"].append((p_damage * (1.0 + expected_burden), row))
        variants["posterior_tail_burden"].append((p_damage * (1.0 + expected_burden) + p_tail * tail_expected, row))
        variants["posterior_event_tail_burden"].append((p_hard * (1.0 + tail_expected), row))
    for scored in variants.values():
        scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return variants


def event_totals(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        event_id = row["event_id"]
        if event_id not in out:
            out[event_id] = {
                "cost": float(row["cost"]),
                "aos": float(row["aos"]),
                "event_hard": bool(row["event_hard"]),
            }
    return out


def selected_metrics(scored: list[tuple[float, dict]], budget: float) -> dict:
    rows = [row for _, row in scored]
    selected_count = max(1, math.ceil(len(rows) * budget))
    selected = [row for _, row in scored[:selected_count]]
    selected_events = event_totals(selected)
    total_events = event_totals(rows)

    total_damage = sum(int(bool(row["part_damage"])) for row in rows)
    total_hard = sum(int(bool(row["event_hard"])) for row in rows)
    selected_damage = sum(int(bool(row["part_damage"])) for row in selected)
    selected_hard = sum(int(bool(row["event_hard"])) for row in selected)
    total_event_cost = sum(item["cost"] for item in total_events.values())
    selected_event_cost = sum(item["cost"] for item in selected_events.values())
    total_event_aos = sum(item["aos"] for item in total_events.values())
    selected_event_aos = sum(item["aos"] for item in selected_events.values())
    overall_damage = total_damage / len(rows) if rows else 0.0
    selected_damage_rate = selected_damage / selected_count if selected_count else 0.0
    overall_hard = total_hard / len(rows) if rows else 0.0
    selected_hard_rate = selected_hard / selected_count if selected_count else 0.0
    return {
        "test_component_records": len(rows),
        "selected_component_records": selected_count,
        "target_records": total_damage,
        "captured_target_records": selected_damage,
        "damage_capture_rate": safe_div(selected_damage, total_damage),
        "selected_damage_rate": selected_damage_rate,
        "damage_lift": safe_div(selected_damage_rate, overall_damage),
        "hard_records": total_hard,
        "captured_hard_records": selected_hard,
        "hard_capture_rate": safe_div(selected_hard, total_hard),
        "selected_hard_rate": selected_hard_rate,
        "hard_lift": safe_div(selected_hard_rate, overall_hard),
        "total_event_cost": total_event_cost,
        "selected_event_cost": selected_event_cost,
        "event_cost_capture_rate": safe_div(selected_event_cost, total_event_cost),
        "total_event_aos": total_event_aos,
        "selected_event_aos": selected_event_aos,
        "event_aos_capture_rate": safe_div(selected_event_aos, total_event_aos),
    }


def aggregate(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "test_years": set(),
        "test_component_records": 0,
        "selected_component_records": 0,
        "target_records": 0,
        "captured_target_records": 0,
        "hard_records": 0,
        "captured_hard_records": 0,
        "total_event_cost": 0.0,
        "selected_event_cost": 0.0,
        "total_event_aos": 0.0,
        "selected_event_aos": 0.0,
        "annual_damage_lifts": [],
        "annual_hard_lifts": [],
    })
    for row in rows:
        key = (row["score"], row["budget_share"])
        item = groups[key]
        item["test_years"].add(row["test_year"])
        for field in [
            "test_component_records",
            "selected_component_records",
            "target_records",
            "captured_target_records",
            "hard_records",
            "captured_hard_records",
        ]:
            item[field] += row[field]
        for field in ["total_event_cost", "selected_event_cost", "total_event_aos", "selected_event_aos"]:
            item[field] += row[field]
        item["annual_damage_lifts"].append(row["damage_lift"])
        item["annual_hard_lifts"].append(row["hard_lift"])
    out = []
    for (score, budget), item in groups.items():
        selected_damage_rate = safe_div(item["captured_target_records"], item["selected_component_records"])
        overall_damage_rate = safe_div(item["target_records"], item["test_component_records"])
        selected_hard_rate = safe_div(item["captured_hard_records"], item["selected_component_records"])
        overall_hard_rate = safe_div(item["hard_records"], item["test_component_records"])
        out.append({
            "score": score,
            "budget_share": budget,
            "test_years": len(item["test_years"]),
            "test_component_records": item["test_component_records"],
            "selected_component_records": item["selected_component_records"],
            "target_records": item["target_records"],
            "captured_target_records": item["captured_target_records"],
            "damage_capture_rate": safe_div(item["captured_target_records"], item["target_records"]),
            "selected_damage_rate": selected_damage_rate,
            "damage_lift": safe_div(selected_damage_rate, overall_damage_rate),
            "hard_records": item["hard_records"],
            "captured_hard_records": item["captured_hard_records"],
            "hard_capture_rate": safe_div(item["captured_hard_records"], item["hard_records"]),
            "selected_hard_rate": selected_hard_rate,
            "hard_lift": safe_div(selected_hard_rate, overall_hard_rate),
            "selected_event_cost": item["selected_event_cost"],
            "total_event_cost": item["total_event_cost"],
            "event_cost_capture_rate": safe_div(item["selected_event_cost"], item["total_event_cost"]),
            "selected_event_aos": item["selected_event_aos"],
            "total_event_aos": item["total_event_aos"],
            "event_aos_capture_rate": safe_div(item["selected_event_aos"], item["total_event_aos"]),
            "mean_annual_damage_lift": sum(item["annual_damage_lifts"]) / len(item["annual_damage_lifts"]),
            "mean_annual_hard_lift": sum(item["annual_hard_lifts"]) / len(item["annual_hard_lifts"]),
        })
    return sorted(out, key=lambda r: (r["budget_share"], -r["damage_lift"], -r["event_cost_capture_rate"]))


def evaluate(parts: list[dict], test_years: list[int]) -> tuple[list[dict], list[dict]]:
    yearly = []
    budgets = [0.05, 0.10]
    for test_year in test_years:
        train = [row for row in parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in parts if int(row["year"]) == test_year]
        if not train or not test:
            continue
        variants = {
            HIER_SCORE: score_hierarchical(train, test, "part_damage"),
            MAIN_SCORE: score_records(train, test, MAIN_SCORE, "part_damage"),
            FREQ_SCORE: score_records(train, test, FREQ_SCORE, "part_damage"),
        }
        variants.update(score_posterior_burden(train, test))
        for score, scored in variants.items():
            for budget in budgets:
                metrics = selected_metrics(scored, budget)
                yearly.append({
                    "test_year": test_year,
                    "score": score,
                    "budget_share": budget,
                    **metrics,
                })
        damage_scores = {
            (row["event_id"], row["component"]): value
            for value, row in variants[HIER_SCORE]
        }
        tail_scores = {
            (row["event_id"], row["component"]): value
            for value, row in variants["posterior_tail_burden"]
        }
        for budget in budgets:
            pool_count = max(1, math.ceil(len(test) * min(0.50, 3.0 * budget)))
            pool_keys = {
                (row["event_id"], row["component"])
                for _, row in sorted(
                    ((damage_scores[(row["event_id"], row["component"])], row) for row in test),
                    key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]),
                )[:pool_count]
            }
            guarded = []
            for row in test:
                row_key = (row["event_id"], row["component"])
                score = tail_scores.get(row_key, 0.0)
                if row_key in pool_keys:
                    guarded.append((1.0 + score, row))
                else:
                    guarded.append((damage_scores.get(row_key, 0.0) * 1e-6, row))
            guarded.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
            metrics = selected_metrics(guarded, budget)
            yearly.append({
                "test_year": test_year,
                "score": "risk_guarded_tail_burden",
                "budget_share": budget,
                **metrics,
            })
    return yearly, aggregate(yearly)


def build_report(aggregate_rows: list[dict]) -> str:
    labels = {
        HIER_SCORE: "Hierarchical damage",
        MAIN_SCORE: "Direct smoothed damage",
        FREQ_SCORE: "Historical frequency",
        "posterior_expected_burden": "Expected posterior burden",
        "posterior_damage_gated_burden": "Damage-gated posterior burden",
        "posterior_tail_burden": "Tail posterior burden",
        "posterior_event_tail_burden": "Event-tail posterior burden",
        "risk_guarded_tail_burden": "Risk-guarded tail burden",
    }
    lines = [
        "# Posterior burden allocation experiment",
        "",
        "Larger damage lift preserves the current main target. Larger cost and AOS capture indicate stronger burden allocation.",
        "",
        "| Budget | Rule | Damage capture | Damage lift | Hard capture | Hard lift | Event cost capture | Event AOS capture |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_rows:
        lines.append(
            f"| {row['budget_share']:.0%} | {labels.get(row['score'], row['score'])} | "
            f"{row['captured_target_records']:,}/{row['target_records']:,} ({row['damage_capture_rate']:.1%}) | "
            f"{row['damage_lift']:.2f} | "
            f"{row['captured_hard_records']:,}/{row['hard_records']:,} ({row['hard_capture_rate']:.1%}) | "
            f"{row['hard_lift']:.2f} | "
            f"{row['event_cost_capture_rate']:.1%} | {row['event_aos_capture_rate']:.1%} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Capacity-constrained posterior burden allocation for wildlife strikes.")
    parser.add_argument("--smoke", action="store_true", help="Use 2024--2025 only.")
    args = parser.parse_args()
    result_dir = RESULT_DIR / ("smoke" if args.smoke else "full")
    test_years = [2024, 2025] if args.smoke else list(range(1995, 2026))
    parts = load_parts()
    yearly, aggregate_rows = evaluate(parts, test_years)
    write_csv(result_dir / "posterior_burden_yearly.csv", yearly)
    write_csv(result_dir / "posterior_burden_aggregate.csv", aggregate_rows)
    (result_dir / "posterior_burden_report.md").write_text(build_report(aggregate_rows), encoding="utf-8")
    print(build_report(aggregate_rows))


if __name__ == "__main__":
    main()
