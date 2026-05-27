from __future__ import annotations

import argparse
import csv
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd
import win32com.client
from catboost import CatBoostClassifier, Pool
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import (  # noqa: E402
    SCORE_SPECS,
    key_for,
    score_records,
    train_scores,
)
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "smoke_tests" / "upgrade_validation"
NTSB_MDB = PROJECT_ROOT / "data" / "raw" / "ntsb_avdata" / "avall" / "avall.mdb"

MAIN_SCORE = "component_phase_size_mass_rate"
FREQ_SCORE = "component_phase_size_mass_frequency"
HIER_SCORE = "hierarchical_component_transition"
EXPOSURE_SCORE = "component_transition_airport_exposure"
LOGISTIC_SCORE = "regularized_categorical_logistic"
CATBOOST_SCORE = "categorical_gradient_boosting"
HIERARCHY_LEVELS = [
    ["component"],
    ["component", "phase_bucket"],
    ["component", "phase_bucket", "size"],
    ["component", "phase_bucket", "size", "aircraft_mass_class"],
]

COMPONENT_TERMS = {
    "engine": ["engine", "eng ", "turbofan", "fan blade", "inlet", "nacelle", "ingest", "ingestion", "compressor"],
    "windshield": ["windshield", "windscreen", "wind screen"],
    "wing_rotor": ["wing", "rotor", "slat", "flap", "aileron", "leading edge"],
    "landing_gear": ["landing gear", "gear", "strut", "brake"],
    "fuselage": ["fuselage", "airframe", "skin"],
    "tail": ["tail", "stabilizer", "rudder", "empennage", "elevator"],
    "nose": ["nose", "radar dome"],
    "radome": ["radome"],
    "propeller": ["propeller", "prop "],
    "lights": ["landing light", "taxi light", "navigation light", "beacon light"],
}

WILDLIFE_TERMS = [
    "bird",
    "birds",
    "birdstrike",
    "bird strike",
    "goose",
    "geese",
    "gull",
    "vulture",
    "hawk",
    "deer",
    "coyote",
    "wildlife",
]

WILDLIFE_REGEX = re.compile(
    r"\b("
    r"bird|birds|birdstrike|bird-strike|bird strike|wildlife|"
    r"deer|goose|geese|gull|duck|hawk|eagle|vulture|turkey|coyote|elk|moose|waterfowl"
    r")\b",
    re.IGNORECASE,
)

LARGE_TERMS = ["deer", "geese", "goose", "vulture", "coyote", "large bird", "flock"]
MEDIUM_TERMS = ["hawk", "duck", "gull", "bird"]

PHASE_PATTERNS = [
    ("departure", ["takeoff", "take-off", "initial climb", "climb", "departure", "takeoff roll", "departed"]),
    ("arrival", ["landing", "approach", "final", "flare", "touchdown", "descent", "landed"]),
    ("ground", ["taxi", "runway", "landing roll"]),
    ("en route", ["en route", "cruise"]),
]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_parts() -> list[dict]:
    events = [enrich(row) for row in load_rows()]
    return [r for r in component_rows(events) if 1990 <= int(r["year"]) <= 2025]


def selected_metrics(scored: list[tuple[float, dict]], target: str, budget: float) -> dict:
    if not scored:
        return {}
    k = max(1, math.ceil(len(scored) * budget))
    selected = [row for _, row in scored[:k]]
    total = sum(int(bool(row[target])) for _, row in scored)
    captured = sum(int(bool(row[target])) for row in selected)
    overall_rate = total / len(scored) if scored else 0.0
    hit_rate = captured / k if k else 0.0
    return {
        "selected_component_records": k,
        "target_records": total,
        "captured_target_records": captured,
        "capture_rate": captured / total if total else 0.0,
        "selected_target_rate": hit_rate,
        "overall_target_rate": overall_rate,
        "lift": hit_rate / overall_rate if overall_rate else 0.0,
    }


def aggregate(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "test_years": set(),
        "test_component_records": 0,
        "target_records": 0,
        "selected_component_records": 0,
        "captured_target_records": 0,
        "annual_lifts": [],
    })
    for row in rows:
        key = (row["experiment"], row["score"], row["budget_share"], row["target"])
        item = groups[key]
        item["test_years"].add(row["test_year"])
        item["test_component_records"] += row["test_component_records"]
        item["target_records"] += row["target_records"]
        item["selected_component_records"] += row["selected_component_records"]
        item["captured_target_records"] += row["captured_target_records"]
        item["annual_lifts"].append(row["lift"])
    out = []
    for key, item in groups.items():
        experiment, score, budget, target = key
        selected_rate = item["captured_target_records"] / item["selected_component_records"]
        overall_rate = item["target_records"] / item["test_component_records"]
        out.append({
            "experiment": experiment,
            "score": score,
            "budget_share": budget,
            "target": target,
            "test_years": len(item["test_years"]),
            "test_component_records": item["test_component_records"],
            "target_records": item["target_records"],
            "selected_component_records": item["selected_component_records"],
            "captured_target_records": item["captured_target_records"],
            "pooled_capture_rate": item["captured_target_records"] / item["target_records"] if item["target_records"] else 0.0,
            "pooled_selected_target_rate": selected_rate,
            "pooled_lift": selected_rate / overall_rate if overall_rate else 0.0,
            "mean_annual_lift": sum(item["annual_lifts"]) / len(item["annual_lifts"]),
        })
    return sorted(out, key=lambda r: (r["experiment"], r["target"], r["budget_share"], -r["pooled_lift"]))


def train_hierarchical_score_tables(train: list[dict], target: str, alpha: float = 10.0) -> tuple[dict[tuple, dict[tuple, float]], float]:
    global_n = len(train)
    global_y = sum(int(bool(r[target])) for r in train)
    global_rate = global_y / global_n if global_n else 0.0
    level_scores: dict[tuple, dict[tuple, float]] = {}
    parent_scores: dict[tuple, float] = {(): global_rate}
    for level in HIERARCHY_LEVELS:
        stats: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "y": 0})
        for row in train:
            k = tuple(row.get(col, "") for col in level)
            stats[k]["n"] += 1
            stats[k]["y"] += int(bool(row[target]))
        scores = {}
        for key, item in stats.items():
            parent_key = key[:-1] if len(key) > 1 else ()
            prior = parent_scores.get(parent_key, global_rate)
            scores[key] = (item["y"] + alpha * prior) / (item["n"] + alpha)
        level_scores[tuple(level)] = scores
        parent_scores = scores
    return level_scores, global_rate


def train_hierarchical_scores(train: list[dict], target: str, alpha: float = 10.0) -> dict[tuple, float]:
    tables, _ = train_hierarchical_score_tables(train, target, alpha)
    return tables.get(tuple(HIERARCHY_LEVELS[-1]), {})


def hierarchical_score_for(row: dict, tables: dict[tuple, dict[tuple, float]], global_rate: float) -> float:
    score = global_rate
    for level in HIERARCHY_LEVELS:
        key = tuple(row.get(col, "") for col in level)
        score = tables.get(tuple(level), {}).get(key, score)
    return score


def score_hierarchical(train: list[dict], test: list[dict], target: str) -> list[tuple[float, dict]]:
    tables, global_rate = train_hierarchical_score_tables(train, target)
    scored = [(hierarchical_score_for(row, tables, global_rate), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def exposure_weight(row: dict, train_airport_counts: dict[tuple, int]) -> float:
    airport = str(row.get("airport_id", "")).upper()
    if airport in {"", "UNKNOWN", "ZZZZ"}:
        return 1.0
    history = sum(train_airport_counts.get((airport, year), 0) for year in range(int(row["year"]) - 5, int(row["year"])))
    if history <= 0:
        return 1.0
    return 1.0 / math.log1p(history)


def score_exposure_adjusted(train: list[dict], test: list[dict], target: str, train_airport_counts: dict[tuple, int]) -> list[tuple[float, dict]]:
    scores = train_scores(
        train,
        SCORE_SPECS[MAIN_SCORE]["keys"],
        target,
        SCORE_SPECS[MAIN_SCORE]["kind"],
    )
    scored = []
    for row in test:
        base = scores.get(key_for(row, SCORE_SPECS[MAIN_SCORE]["keys"]), 0.0)
        scored.append((base * exposure_weight(row, train_airport_counts), row))
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def logistic_feature_dict(row: dict) -> dict[str, int]:
    component = str(row.get("component", ""))
    phase = str(row.get("phase_bucket", ""))
    size = str(row.get("size", ""))
    mass = str(row.get("aircraft_mass_class", ""))
    species = str(row.get("species_id", "UNKNOWN"))
    return {
        f"component={component}": 1,
        f"phase={phase}": 1,
        f"size={size}": 1,
        f"mass={mass}": 1,
        f"species={species}": 1,
        f"component_phase={component}|{phase}": 1,
        f"component_size={component}|{size}": 1,
        f"phase_size={phase}|{size}": 1,
        f"component_phase_size={component}|{phase}|{size}": 1,
        f"component_phase_size_mass={component}|{phase}|{size}|{mass}": 1,
        f"species_phase_size={species}|{phase}|{size}": 1,
    }


def score_regularized_logistic(train: list[dict], test: list[dict], target: str) -> list[tuple[float, dict]]:
    y = [int(bool(row[target])) for row in train]
    if len(set(y)) < 2:
        scored = [(0.0, row) for row in test]
        scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
        return scored
    vectorizer = DictVectorizer(sparse=True)
    x_train = vectorizer.fit_transform(logistic_feature_dict(row) for row in train)
    x_test = vectorizer.transform(logistic_feature_dict(row) for row in test)
    model = LogisticRegression(
        C=0.5,
        penalty="l2",
        solver="liblinear",
        max_iter=200,
        class_weight="balanced",
        random_state=20260513,
    )
    model.fit(x_train, y)
    probabilities = model.predict_proba(x_test)[:, 1]
    scored = [(float(score), row) for score, row in zip(probabilities, test)]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


CATBOOST_FEATURES = [
    "component",
    "phase_bucket",
    "size",
    "aircraft_mass_class",
    "species_id",
]


def catboost_frame(rows: list[dict]) -> pd.DataFrame:
    data = []
    for row in rows:
        data.append({
            feature: str(row.get(feature, "UNKNOWN") or "UNKNOWN")
            for feature in CATBOOST_FEATURES
        })
    return pd.DataFrame(data, columns=CATBOOST_FEATURES)


def score_categorical_gradient_boosting(train: list[dict], test: list[dict], target: str) -> list[tuple[float, dict]]:
    y = [int(bool(row[target])) for row in train]
    if len(set(y)) < 2:
        scored = [(0.0, row) for row in test]
        scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
        return scored
    x_train = catboost_frame(train)
    x_test = catboost_frame(test)
    positives = sum(y)
    negatives = len(y) - positives
    scale_pos_weight = negatives / positives if positives else 1.0
    model = CatBoostClassifier(
        iterations=220,
        depth=6,
        learning_rate=0.045,
        loss_function="Logloss",
        eval_metric="Logloss",
        l2_leaf_reg=8.0,
        random_seed=20260513,
        thread_count=-1,
        verbose=False,
        allow_writing_files=False,
        scale_pos_weight=scale_pos_weight,
    )
    train_pool = Pool(x_train, label=y, cat_features=CATBOOST_FEATURES)
    test_pool = Pool(x_test, cat_features=CATBOOST_FEATURES)
    model.fit(train_pool)
    probabilities = model.predict_proba(test_pool)[:, 1]
    scored = [(float(score), row) for score, row in zip(probabilities, test)]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def rolling_validation(parts: list[dict], test_years: list[int], experiment: str) -> list[dict]:
    rows = []
    budgets = [0.05, 0.10]
    target = "part_damage"
    airport_counts = defaultdict(int)
    seen_events = set()
    for row in parts:
        key = (row["event_id"], row["airport_id"], row["year"])
        if key in seen_events:
            continue
        seen_events.add(key)
        airport_counts[(str(row["airport_id"]).upper(), int(row["year"]))] += 1

    for test_year in test_years:
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [r for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue
        scored_variants = {
            MAIN_SCORE: score_records(train, test, MAIN_SCORE, target),
            FREQ_SCORE: score_records(train, test, FREQ_SCORE, target),
            HIER_SCORE: score_hierarchical(train, test, target),
            EXPOSURE_SCORE: score_exposure_adjusted(train, test, target, airport_counts),
            LOGISTIC_SCORE: score_regularized_logistic(train, test, target),
            CATBOOST_SCORE: score_categorical_gradient_boosting(train, test, target),
        }
        for score_name, scored in scored_variants.items():
            for budget in budgets:
                metrics = selected_metrics(scored, target, budget)
                rows.append({
                    "experiment": experiment,
                    "test_year": test_year,
                    "target": target,
                    "score": score_name,
                    "budget_share": budget,
                    "test_component_records": len(test),
                    **metrics,
                })
    return rows


def negative_controls(parts: list[dict], test_years: list[int]) -> list[dict]:
    rng = random.Random(20260512)
    target = "part_damage"
    budgets = [0.05, 0.10]
    rows = []
    for test_year in test_years:
        train = [dict(r) for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [dict(r) for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue

        test_labels = [bool(r[target]) for r in test]
        rng.shuffle(test_labels)
        test_shuffled = [dict(r) for r in test]
        for row, label in zip(test_shuffled, test_labels):
            row[target] = label

        scored_actual = score_records(train, test, MAIN_SCORE, target)
        score_values = [score for score, _ in scored_actual]
        rng.shuffle(score_values)
        scored_random_score = [(score, row) for score, (_, row) in zip(score_values, scored_actual)]
        scored_random_score.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))

        control_scores = {
            "test_label_shuffle": score_records(train, test_shuffled, MAIN_SCORE, target),
            "score_shuffle": scored_random_score,
        }

        for name, scored in control_scores.items():
            for budget in budgets:
                metrics = selected_metrics(scored, target, budget)
                rows.append({
                    "experiment": name,
                    "test_year": test_year,
                    "target": target,
                    "score": MAIN_SCORE,
                    "budget_share": budget,
                    "test_component_records": len(scored),
                    **metrics,
                })
    return rows


def ntbs_connect():
    conn = win32com.client.Dispatch("ADODB.Connection")
    conn.Open(f"Provider=Microsoft.ACE.OLEDB.16.0;Data Source={NTSB_MDB};Persist Security Info=False;")
    return conn


def rows_from_recordset(rs) -> Iterable[dict]:
    fields = [rs.Fields(i).Name for i in range(rs.Fields.Count)]
    while not rs.EOF:
        yield {fields[i]: rs.Fields(i).Value for i in range(len(fields))}
        rs.MoveNext()


def normalize_text(*values: object) -> str:
    return " ".join(str(v or "") for v in values).lower()


def infer_component(blob: str) -> str:
    hits = []
    for component, terms in COMPONENT_TERMS.items():
        if any(term in blob for term in terms):
            hits.append(component)
    if "engine" in hits:
        return "engine"
    return hits[0] if hits else "other"


def infer_phase(blob: str) -> str:
    for phase, terms in PHASE_PATTERNS:
        if any(term in blob for term in terms):
            return phase
    return "unknown"


def infer_size(blob: str) -> str:
    if any(term in blob for term in LARGE_TERMS):
        return "LARGE"
    if any(term in blob for term in MEDIUM_TERMS):
        return "MEDIUM"
    return "UNKNOWN"


def infer_mass_class(weight: object) -> str:
    try:
        pounds = float(weight)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if pounds <= 0:
        return "UNKNOWN"
    kg = pounds * 0.45359237
    if kg <= 2250:
        return "1"
    if kg <= 5700:
        return "2"
    if kg <= 27000:
        return "3"
    if kg <= 272000:
        return "4"
    return "5"


def load_ntsb_wildlife_rows(start_year: int = 1990, end_year: int = 2026) -> list[dict]:
    conn = ntbs_connect()
    term_clauses = []
    for term in WILDLIFE_TERMS:
        escaped = term.replace("'", "''")
        term_clauses.extend([
            f"n.narr_accp LIKE '%{escaped}%'",
            f"n.narr_accf LIKE '%{escaped}%'",
            f"n.narr_cause LIKE '%{escaped}%'",
            f"n.narr_inc LIKE '%{escaped}%'",
            f"f.finding_description LIKE '%{escaped}%'",
        ])
    where_terms = " OR ".join(term_clauses)
    query = f"""
        SELECT e.ev_id, e.ev_year, e.ev_month, e.ev_state, e.apt_name, e.ev_nr_apt_id,
               e.ev_highest_injury, e.inj_tot_f, e.inj_tot_s,
               a.Aircraft_Key, a.damage, a.cert_max_gr_wt, a.acft_make, a.acft_model,
               n.narr_accp, n.narr_accf, n.narr_cause, n.narr_inc,
               f.finding_description
        FROM ((events AS e
        INNER JOIN aircraft AS a ON e.ev_id = a.ev_id)
        LEFT JOIN narratives AS n ON a.ev_id = n.ev_id AND a.Aircraft_Key = n.Aircraft_Key)
        LEFT JOIN Findings AS f ON a.ev_id = f.ev_id AND a.Aircraft_Key = f.Aircraft_Key
        WHERE e.ev_year >= {start_year} AND e.ev_year <= {end_year}
          AND ({where_terms})
    """
    rs = conn.Execute(query)[0]
    grouped: dict[tuple, dict] = {}
    for row in rows_from_recordset(rs):
        key = (row.get("ev_id"), row.get("Aircraft_Key"))
        item = grouped.setdefault(key, {**row, "finding_texts": []})
        item["finding_texts"].append(row.get("finding_description") or "")
    rs.Close()
    conn.Close()

    out = []
    for item in grouped.values():
        blob = normalize_text(
            item.get("narr_accp"),
            item.get("narr_accf"),
            item.get("narr_cause"),
            item.get("narr_inc"),
            " ".join(item.get("finding_texts", [])),
        )
        if not WILDLIFE_REGEX.search(blob):
            continue
        damage = str(item.get("damage") or "").upper()
        severe = damage in {"SUBS", "DEST"} or int(item.get("inj_tot_f") or 0) > 0 or int(item.get("inj_tot_s") or 0) > 0
        out.append({
            "ev_id": item.get("ev_id"),
            "year": int(item.get("ev_year") or 0),
            "month": int(item.get("ev_month") or 0),
            "airport_id": text(item.get("ev_nr_apt_id")).upper(),
            "state": text(item.get("ev_state")).upper(),
            "damage": damage,
            "severe": severe,
            "component": infer_component(blob),
            "phase_bucket": infer_phase(blob),
            "size": infer_size(blob),
            "aircraft_mass_class": infer_mass_class(item.get("cert_max_gr_wt")),
            "engine_text": int(any(term in blob for term in ["engine", "ingest", "ingestion", "fan blade", "nacelle"])),
            "return_text": int(any(term in blob for term in ["return", "declared an emergency", "emergency", "landed safely", "airport rescue"])),
        })
    return sorted(out, key=lambda r: (r["year"], r["ev_id"]))


def ntsb_external_enrichment(parts: list[dict]) -> tuple[list[dict], list[dict]]:
    train = [r for r in parts if 2016 <= int(r["year"]) <= 2025]
    keys = SCORE_SPECS[MAIN_SCORE]["keys"]
    ntsb_rows = load_ntsb_wildlife_rows(1990, 2025)
    scored = []
    score_tables = {
        "component_damage_score": train_scores(train, keys, "part_damage", "rate"),
        "event_consequence_score": train_scores(train, keys, "event_hard", "rate"),
    }
    hier_tables, hier_global = train_hierarchical_score_tables(train, "part_damage")
    for row in ntsb_rows:
        key = tuple(row.get(k, "") for k in keys)
        row = dict(row)
        for score_name, table in score_tables.items():
            row[score_name] = table.get(key, 0.0)
        row["hierarchical_component_damage_score"] = hierarchical_score_for(row, hier_tables, hier_global)
        scored.append(row)

    validation_rows = []
    all_score_names = list(score_tables) + ["hierarchical_component_damage_score"]
    for score_name in all_score_names:
        if score_name == "hierarchical_component_damage_score":
            nw_scores = sorted([hierarchical_score_for(row, hier_tables, hier_global) for row in train], reverse=True)
        else:
            table = score_tables[score_name]
            nw_scores = sorted([table.get(key_for(row, keys), 0.0) for row in train], reverse=True)
        for share in [0.05, 0.10, 0.20]:
            if not nw_scores or not scored:
                continue
            k = max(1, math.ceil(len(nw_scores) * share))
            cutoff = nw_scores[k - 1]
            nw_top = sum(1 for value in nw_scores if value >= cutoff)
            nw_rest = len(nw_scores) - nw_top
            top = [r for r in scored if r[score_name] >= cutoff]
            rest = [r for r in scored if r[score_name] < cutoff]
            top_severe = sum(int(r["severe"]) for r in top)
            rest_severe = sum(int(r["severe"]) for r in rest)
            top_mass4 = [r for r in top if r["aircraft_mass_class"] == "4"]
            mass4 = [r for r in scored if r["aircraft_mass_class"] == "4"]
            ext_top = len(top)
            ext_rest = len(rest)
            enrichment_odds_ratio = ((ext_top + 0.5) * (nw_rest + 0.5)) / ((ext_rest + 0.5) * (nw_top + 0.5))
            validation_rows.append({
                "score": score_name,
                "nw_review_share": share,
                "nw_reference_share_with_ties": nw_top / len(nw_scores),
                "score_cutoff": cutoff,
                "records": len(scored),
                "top_records": len(top),
                "top_record_share": len(top) / len(scored),
                "severe_records": sum(int(r["severe"]) for r in scored),
                "top_severe_records": top_severe,
                "top_severe_capture_rate": top_severe / sum(int(r["severe"]) for r in scored) if scored else 0.0,
                "top_severe_rate": top_severe / len(top) if top else 0.0,
                "rest_severe_rate": rest_severe / len(rest) if rest else 0.0,
                "enrichment_odds_ratio": enrichment_odds_ratio,
                "engine_text_capture": sum(int(r["engine_text"]) for r in top) / sum(int(r["engine_text"]) for r in scored) if sum(int(r["engine_text"]) for r in scored) else 0.0,
                "mass4_records": len(mass4),
                "top_mass4_records": len(top_mass4),
                "top_mass4_capture_rate": len(top_mass4) / len(mass4) if mass4 else 0.0,
            })
    examples = sorted(scored, key=lambda r: (-r["component_damage_score"], -int(r["severe"]), r["year"]))[:40]
    return validation_rows, examples


def build_report(aggregate_rows: list[dict], negative_rows: list[dict], external_rows: list[dict]) -> str:
    lines = [
        "# Upgrade validation smoke test",
        "",
        "## Recent rolling allocation",
        "",
        "| Experiment | Score | Budget | Captured | Capture | Hit rate | Lift |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_rows:
        if row["experiment"] not in {"recent_rolling_smoke", "full_rolling_validation"}:
            continue
        lines.append(
            f"| {row['experiment']} | {row['score']} | {row['budget_share']:.0%} | "
            f"{row['captured_target_records']:,}/{row['target_records']:,} | "
            f"{row['pooled_capture_rate']:.1%} | {row['pooled_selected_target_rate']:.1%} | "
            f"{row['pooled_lift']:.2f} |"
        )

    lines.extend(["", "## Negative controls", "", "| Control | Budget | Lift |", "|---|---:|---:|"])
    for row in negative_rows:
        lines.append(f"| {row['experiment']} | {row['budget_share']:.0%} | {row['pooled_lift']:.2f} |")

    lines.extend(["", "## NTSB external consistency", "", "| Score | NWSD review share | External severe capture | External top share | Consistency OR | Engine-text capture | Mass-class-4 capture |", "|---|---:|---:|---:|---:|---:|---:|"])
    for row in external_rows:
        lines.append(
            f"| {row['score']} | {row['nw_review_share']:.0%} | {row['top_severe_records']:,}/{row['severe_records']:,} "
            f"({row['top_severe_capture_rate']:.1%}) | {row['top_record_share']:.1%} | "
            f"{row['enrichment_odds_ratio']:.2f} | {row['engine_text_capture']:.1%} | "
            f"{row['top_mass4_records']:,}/{row['mass4_records']:,} ({row['top_mass4_capture_rate']:.1%}) |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run upgrade validation checks for FAA wildlife component review.")
    parser.add_argument("--full", action="store_true", help="Run the 1995--2025 rolling validation instead of the recent smoke window.")
    args = parser.parse_args()

    result_dir = PROJECT_ROOT / "results" / "experiments" / "upgrade_validation" if args.full else RESULT_DIR
    result_dir.mkdir(parents=True, exist_ok=True)
    parts = load_parts()
    test_years = list(range(1995, 2026)) if args.full else [2023, 2024, 2025]
    negative_years = list(range(1995, 2026)) if args.full else [2024, 2025]
    experiment = "full_rolling_validation" if args.full else "recent_rolling_smoke"

    recent_rows = rolling_validation(parts, test_years, experiment)
    negative_yearly = negative_controls(parts, negative_years)
    negative_aggregate = aggregate(negative_yearly)
    recent_aggregate = aggregate(recent_rows)
    external_rows, external_examples = ntsb_external_enrichment(parts)

    write_csv(result_dir / "upgrade_rolling_yearly.csv", recent_rows)
    write_csv(result_dir / "upgrade_rolling_aggregate.csv", recent_aggregate)
    write_csv(result_dir / "negative_controls_yearly.csv", negative_yearly)
    write_csv(result_dir / "negative_controls_aggregate.csv", negative_aggregate)
    write_csv(result_dir / "ntsb_external_enrichment.csv", external_rows)
    write_csv(result_dir / "ntsb_external_examples.csv", external_examples)

    report = build_report(recent_aggregate, negative_aggregate, external_rows)
    (result_dir / "upgrade_validation_report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
