from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atads_exposure_validation import normalized_airport  # noqa: E402
from rolling_faa_wildlife_component_review import (  # noqa: E402
    SCORE_SPECS,
    key_for,
    score_records,
    train_scores,
)
from jsr_weather_smoke import (  # noqa: E402
    ISD_HISTORY,
    NOAA_ACCESS_URL,
    download_weather,
    load_weather_series,
    nearest_weather,
    parse_incident_datetime,
    station_lookup,
)
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from smoke_upgrade_validation import (  # noqa: E402
    COMPONENT_TERMS,
    HIERARCHY_LEVELS,
    LARGE_TERMS,
    MEDIUM_TERMS,
    PHASE_PATTERNS,
    WILDLIFE_REGEX,
    hierarchical_score_for,
    infer_component,
    infer_mass_class,
    infer_phase,
    infer_size,
    normalize_text,
    ntbs_connect,
    rows_from_recordset,
    train_hierarchical_score_tables,
)
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOP_AIRPORTS = PROJECT_ROOT / "data" / "metadata" / "top_nwsd_airports_120.txt"
GBIF_CACHE = PROJECT_ROOT / "data" / "raw" / "gbif" / "airport_month_bird_counts.csv"
MAIN_SCORE = "component_phase_size_mass_rate"
FREQ_SCORE = "component_phase_size_mass_frequency"
TARGET = "part_damage"
GBIF_OCCURRENCE_URL = "https://api.gbif.org/v1/occurrence/search"
GBIF_AVES_TAXON_KEY = "212"


def result_dir(smoke: bool) -> Path:
    base = PROJECT_ROOT / "results" / ("smoke_tests" if smoke else "experiments")
    return base / "jsr_priority"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_events() -> list[dict]:
    return [enrich(row) for row in load_rows()]


def load_parts() -> list[dict]:
    return [row for row in component_rows(load_events()) if 1990 <= int(row["year"]) <= 2025]


def read_top_airports(limit: int | None = None) -> list[str]:
    raw = TOP_AIRPORTS.read_text(encoding="utf-8").strip()
    airports = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return airports[:limit] if limit else airports


def selected_metrics(scored: list[tuple[float, dict]], target: str, budget: float, event_dedup: bool = False) -> dict:
    if not scored:
        return {}
    k = max(1, math.ceil(len(scored) * budget))
    selected = [row for _, row in scored[:k]]
    population = [row for _, row in scored]
    total = sum(int(bool(row[target])) for row in population)
    captured = sum(int(bool(row[target])) for row in selected)
    selected_rate = captured / k
    overall_rate = total / len(population) if population else 0.0

    def event_burden(rows: list[dict]) -> tuple[float, float]:
        seen = set()
        cost = 0.0
        aos = 0.0
        for row in rows:
            event_id = row["event_id"]
            if event_id in seen:
                continue
            seen.add(event_id)
            cost += float(row.get("cost") or 0.0)
            aos += float(row.get("aos") or 0.0)
        return cost, aos

    selected_cost, selected_aos = event_burden(selected)
    total_cost, total_aos = event_burden(population)
    return {
        "test_component_records": len(population),
        "target_records": total,
        "selected_component_records": k,
        "captured_target_records": captured,
        "capture_rate": captured / total if total else 0.0,
        "selected_target_rate": selected_rate,
        "overall_target_rate": overall_rate,
        "lift": selected_rate / overall_rate if overall_rate else 0.0,
        "event_deduplicated_cost_capture": selected_cost / total_cost if total_cost else 0.0,
        "event_deduplicated_aos_capture": selected_aos / total_aos if total_aos else 0.0,
        "selected_event_deduplicated_cost": selected_cost,
        "total_event_deduplicated_cost": total_cost,
        "selected_event_deduplicated_aos": selected_aos,
        "total_event_deduplicated_aos": total_aos,
    }


def aggregate_yearly(rows: list[dict], group_cols: list[str]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[tuple(row[col] for col in group_cols)].append(row)
    out = []
    for key, subset in groups.items():
        selected = sum(row["selected_component_records"] for row in subset)
        captured = sum(row["captured_target_records"] for row in subset)
        target = sum(row["target_records"] for row in subset)
        total = sum(row["test_component_records"] for row in subset)
        selected_rate = captured / selected if selected else 0.0
        overall_rate = target / total if total else 0.0
        item = {col: value for col, value in zip(group_cols, key)}
        item.update({
            "test_years": len({row["test_year"] for row in subset}),
            "test_component_records": total,
            "target_records": target,
            "selected_component_records": selected,
            "captured_target_records": captured,
            "capture_rate": captured / target if target else 0.0,
            "selected_target_rate": selected_rate,
            "overall_target_rate": overall_rate,
            "lift": selected_rate / overall_rate if overall_rate else 0.0,
            "event_deduplicated_cost_capture": (
                sum(row["selected_event_deduplicated_cost"] for row in subset)
                / sum(row["total_event_deduplicated_cost"] for row in subset)
                if sum(row["total_event_deduplicated_cost"] for row in subset) else 0.0
            ),
            "event_deduplicated_aos_capture": (
                sum(row["selected_event_deduplicated_aos"] for row in subset)
                / sum(row["total_event_deduplicated_aos"] for row in subset)
                if sum(row["total_event_deduplicated_aos"] for row in subset) else 0.0
            ),
        })
        out.append(item)
    return sorted(out, key=lambda r: tuple(str(r[col]) for col in group_cols))


def score_hierarchical(train: list[dict], test: list[dict]) -> list[tuple[float, dict]]:
    tables, global_rate = train_hierarchical_score_tables(train, TARGET)
    scored = [(hierarchical_score_for(row, tables, global_rate), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def score_frequency(train: list[dict], test: list[dict]) -> list[tuple[float, dict]]:
    return score_records(train, test, FREQ_SCORE, TARGET)


def budget_frontier(parts: list[dict], test_years: list[int], budgets: list[float]) -> tuple[list[dict], list[dict]]:
    yearly = []
    for test_year in test_years:
        train = [row for row in parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in parts if int(row["year"]) == test_year]
        if not train or not test:
            continue
        scored_sets = {
            "hierarchical_review_rule": score_hierarchical(train, test),
            "historical_frequency": score_frequency(train, test),
        }
        for score_name, scored in scored_sets.items():
            for budget in budgets:
                yearly.append({
                    "test_year": test_year,
                    "score": score_name,
                    "budget_share": budget,
                    **selected_metrics(scored, TARGET, budget),
                })
    aggregate = aggregate_yearly(yearly, ["score", "budget_share"])
    return yearly, aggregate


def source_group(value: object) -> str:
    s = text(value).upper()
    if not s:
        return "unknown source"
    if "FAA" in s:
        return "FAA source"
    if "AIRPORT" in s:
        return "airport source"
    if "AIR" in s or "OPERATOR" in s or "PILOT" in s:
        return "operator or pilot source"
    return "other source"


def is_us_airport(row: dict) -> bool:
    airport = text(row.get("airport_id")).upper()
    state = text(row.get("state")).upper()
    if len(airport) == 3 and airport != "ZZZ":
        return True
    if len(airport) == 4 and airport.startswith("K"):
        return True
    return len(state) == 2 and state not in {"", "ZZ"}


def add_bias_fields(parts: list[dict]) -> list[dict]:
    out = []
    for row in parts:
        item = dict(row)
        species = text(item.get("species_id") or item.get("species")).upper()
        size = text(item.get("size")).upper()
        item["bias_dimension_species"] = "known species" if species not in {"", "UNKNOWN", "UNKBS", "UNKBM", "UNKBL"} else "unknown species"
        item["bias_dimension_size"] = "known size" if size not in {"", "UNKNOWN"} else "unknown size"
        item["bias_dimension_airport"] = "United States airport" if is_us_airport(item) else "non-US or unknown airport"
        item["bias_dimension_source"] = source_group(item.get("source"))
        year = int(item["year"])
        if year <= 2008:
            item["bias_dimension_period"] = "1995-2008"
        elif year <= 2019:
            item["bias_dimension_period"] = "2009-2019"
        else:
            item["bias_dimension_period"] = "2020-2025"
        out.append(item)
    return out


def reporting_bias_strata(parts: list[dict], test_years: list[int], budgets: list[float]) -> tuple[list[dict], list[dict]]:
    parts = add_bias_fields(parts)
    dimensions = [
        "bias_dimension_species",
        "bias_dimension_size",
        "bias_dimension_airport",
        "bias_dimension_source",
        "bias_dimension_period",
    ]
    yearly = []
    for test_year in test_years:
        train = [row for row in parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in parts if int(row["year"]) == test_year]
        if not train or not test:
            continue
        scored_all = score_hierarchical(train, test)
        for dimension in dimensions:
            levels = sorted({row[dimension] for row in test})
            for level in levels:
                scoped = [(score, row) for score, row in scored_all if row[dimension] == level]
                if len(scoped) < 50 or sum(int(bool(row[TARGET])) for _, row in scoped) == 0:
                    continue
                for budget in budgets:
                    yearly.append({
                        "test_year": test_year,
                        "dimension": dimension.replace("bias_dimension_", ""),
                        "stratum": level,
                        "score": "hierarchical_review_rule",
                        "budget_share": budget,
                        **selected_metrics(scoped, TARGET, budget),
                    })
    aggregate = aggregate_yearly(yearly, ["dimension", "stratum", "budget_share"])
    return yearly, aggregate


def ntsb_serious_query(non_wildlife: bool, start_year: int = 1990, end_year: int = 2025) -> list[dict]:
    conn = ntbs_connect()
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
          AND (a.damage IN ('SUBS','DEST') OR e.inj_tot_f > 0 OR e.inj_tot_s > 0)
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
        wildlife = bool(WILDLIFE_REGEX.search(blob))
        if non_wildlife and wildlife:
            continue
        if (not non_wildlife) and (not wildlife):
            continue
        component = infer_component(blob)
        component_hit = any(term in blob for term in COMPONENT_TERMS.get(component, []))
        if not component_hit:
            continue
        out.append({
            "ev_id": item.get("ev_id"),
            "year": int(item.get("ev_year") or 0),
            "record_type": "non_wildlife_serious" if non_wildlife else "wildlife_serious",
            "component": component,
            "phase_bucket": infer_phase(blob),
            "aircraft_mass_class": infer_mass_class(item.get("cert_max_gr_wt")),
            "size": infer_size(blob) if not non_wildlife else "UNKNOWN",
            "damage": text(item.get("damage")).upper(),
        })
    return sorted(out, key=lambda r: (r["year"], str(r["ev_id"])))


def projected_cell(row: dict) -> tuple:
    return (
        row.get("component", ""),
        row.get("phase_bucket", ""),
        row.get("aircraft_mass_class", ""),
    )


def strict_cell(row: dict) -> tuple:
    return (
        row.get("component", ""),
        row.get("phase_bucket", ""),
        row.get("size", ""),
        row.get("aircraft_mass_class", ""),
    )


def year_band(year: int) -> str:
    if year < 2000:
        return "1990-1999"
    if year < 2010:
        return "2000-2009"
    if year < 2020:
        return "2010-2019"
    return "2020-2025"


def matched_key(row: dict) -> tuple:
    return (
        row.get("component", ""),
        row.get("phase_bucket", ""),
        row.get("aircraft_mass_class", ""),
        year_band(int(row.get("year") or 0)),
    )


def parent_from_strict(cell: tuple) -> tuple:
    return (cell[0], cell[1], cell[3])


def ntsb_stress_check(parts: list[dict]) -> tuple[list[dict], list[dict]]:
    train = [row for row in parts if 2016 <= int(row["year"]) <= 2025]
    train_scored = score_hierarchical(train, train)
    external_sets = {
        "wildlife serious direct text": ntsb_serious_query(non_wildlife=False),
        "non-wildlife serious direct text": ntsb_serious_query(non_wildlife=True),
    }
    summary = []
    examples = []
    specifications = [
        ("strict component-phase-size-mass cell types", strict_cell, "cell_type_budget"),
        ("wide component-phase-mass projection", projected_cell, "record_budget_projection"),
    ]
    for specification, cell_fn, budget_type in specifications:
        for budget in [0.05, 0.10]:
            if budget_type == "cell_type_budget":
                cell_scores: dict[tuple, float] = {}
                for score, row in train_scored:
                    cell = cell_fn(row)
                    cell_scores[cell] = max(cell_scores.get(cell, 0.0), score)
                ordered_cells = sorted(cell_scores, key=lambda cell: (-cell_scores[cell], cell))
                selected_cells = set(ordered_cells[:max(1, math.ceil(len(ordered_cells) * budget))])
            else:
                k = max(1, math.ceil(len(train_scored) * budget))
                selected_cells = {cell_fn(row) for _, row in train_scored[:k]}
            nw_top = sum(1 for _, row in train_scored if cell_fn(row) in selected_cells)
            nw_total = len(train_scored)
            nw_share = nw_top / nw_total
            for set_name, rows in external_sets.items():
                top_rows = [row for row in rows if cell_fn(row) in selected_cells]
                ext_top = len(top_rows)
                ext_total = len(rows)
                ext_share = ext_top / ext_total if ext_total else 0.0
                ext_rest = ext_total - ext_top
                nw_rest = nw_total - nw_top
                odds_ratio = ((ext_top + 0.5) * (nw_rest + 0.5)) / ((ext_rest + 0.5) * (nw_top + 0.5)) if ext_total else 0.0
                summary.append({
                    "specification": specification,
                    "external_set": set_name,
                    "budget_share": budget,
                    "selected_cell_count": len(selected_cells),
                    "external_records": ext_total,
                    "external_records_in_selected_cells": ext_top,
                    "external_selected_share": ext_share,
                    "nwsd_reference_share": nw_share,
                    "cell_odds_ratio": odds_ratio,
                })
                for row in top_rows[:15]:
                    item = dict(row)
                    item["budget_share"] = budget
                    item["external_set"] = set_name
                    item["specification"] = specification
                    examples.append(item)
    return summary, examples


def ntsb_matched_stress_check(parts: list[dict]) -> list[dict]:
    train = [row for row in parts if 2016 <= int(row["year"]) <= 2025]
    train_scored = score_hierarchical(train, train)
    wildlife_rows = ntsb_serious_query(non_wildlife=False)
    nonwildlife_rows = ntsb_serious_query(non_wildlife=True)
    nonwild_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in nonwildlife_rows:
        nonwild_by_key[matched_key(row)].append(row)
    for rows in nonwild_by_key.values():
        rows.sort(key=lambda r: (int(r.get("year") or 0), str(r.get("ev_id"))))

    matched_wildlife = []
    matched_nonwildlife = []
    for row in wildlife_rows:
        controls = nonwild_by_key.get(matched_key(row), [])
        if not controls:
            continue
        matched_wildlife.append(row)
    wildlife_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in matched_wildlife:
        wildlife_by_key[matched_key(row)].append(row)
    for key, cases in wildlife_by_key.items():
        controls = nonwild_by_key.get(key, [])
        matched_nonwildlife.extend(controls[: min(len(controls), len(cases) * 3)])

    rows_out = []
    for budget in [0.05, 0.10]:
        cell_scores: dict[tuple, float] = {}
        for score, row in train_scored:
            cell = strict_cell(row)
            cell_scores[cell] = max(cell_scores.get(cell, 0.0), score)
        ordered_cells = sorted(cell_scores, key=lambda cell: (-cell_scores[cell], cell))
        selected_strict = set(ordered_cells[:max(1, math.ceil(len(ordered_cells) * budget))])
        selected_parents = {parent_from_strict(cell) for cell in selected_strict}

        wild_strict = sum(1 for row in matched_wildlife if strict_cell(row) in selected_strict)
        wild_parent = sum(1 for row in matched_wildlife if projected_cell(row) in selected_parents)
        control_parent = sum(1 for row in matched_nonwildlife if projected_cell(row) in selected_parents)
        wild_total = len(matched_wildlife)
        control_total = len(matched_nonwildlife)
        wild_parent_out = wild_total - wild_parent
        control_parent_out = control_total - control_parent
        parent_case_control_or = (
            ((wild_parent + 0.5) * (control_parent_out + 0.5))
            / ((wild_parent_out + 0.5) * (control_parent + 0.5))
            if wild_total and control_total else 0.0
        )
        rows_out.append({
            "budget_share": budget,
            "matching_key": "component-phase-mass-year_band",
            "wildlife_records_with_match": wild_total,
            "matched_nonwildlife_records": control_total,
            "wildlife_strict_selected_records": wild_strict,
            "wildlife_strict_selected_share": wild_strict / wild_total if wild_total else 0.0,
            "wildlife_parent_selected_records": wild_parent,
            "wildlife_parent_selected_share": wild_parent / wild_total if wild_total else 0.0,
            "matched_nonwildlife_parent_selected_records": control_parent,
            "matched_nonwildlife_parent_selected_share": control_parent / control_total if control_total else 0.0,
            "parent_case_control_or": parent_case_control_or,
        })
    return rows_out


def airport_icao(code: str) -> str:
    code = code.upper()
    if len(code) == 4:
        return code
    if len(code) == 3:
        return "K" + code
    return code


def weather_match_parts(
    parts: list[dict],
    airports: list[str],
    years: list[int],
    cache_only: bool,
) -> tuple[list[dict], list[dict]]:
    stations = station_lookup()
    airport_station = {airport: stations.get(airport_icao(airport)) for airport in airports}
    matched = []
    coverage = []
    parts_by_airport_year: dict[tuple[str, int], list[dict]] = defaultdict(list)
    airport_set = set(airports)
    for row in parts:
        airport = normalized_airport(row.get("airport_id"))
        year = int(row["year"])
        if airport in airport_set and year in set(years):
            parts_by_airport_year[(airport, year)].append(row)

    for airport in airports:
        station = airport_station.get(airport)
        for year in years:
            candidate_rows = parts_by_airport_year.get((airport, year), [])
            series = load_weather_series(station, year, cache_only=cache_only) if station else []
            stamps = [row["timestamp"] for row in series]
            matched_count = 0
            for row in candidate_rows:
                stamp = parse_incident_datetime(row)
                if not stamp:
                    continue
                obs, minutes = nearest_weather(stamps, series, stamp)
                if not obs:
                    continue
                item = dict(row)
                item["airport_norm"] = airport
                item["weather_station"] = station
                item["weather_match_minutes"] = round(float(minutes), 1)
                for key in [
                    "wind_speed_mps",
                    "visibility_m",
                    "ceiling_m",
                    "temperature_c",
                    "precip_mm",
                    "weather_severity",
                    "adverse_weather",
                ]:
                    item[key] = obs.get(key)
                item["weather_stratum"] = "adverse weather" if obs.get("adverse_weather") else "normal weather"
                item["high_wind"] = int((obs.get("wind_speed_mps") or 0.0) >= 10.0)
                item["low_visibility"] = int(obs.get("visibility_m") is not None and float(obs.get("visibility_m")) < 5000.0)
                item["precipitation"] = int((obs.get("precip_mm") or 0.0) > 0.0)
                item["low_ceiling"] = int(obs.get("ceiling_m") is not None and float(obs.get("ceiling_m")) < 1000.0)
                matched.append(item)
                matched_count += 1
            coverage.append({
                "airport_id": airport,
                "year": year,
                "station": station or "",
                "candidate_component_records": len(candidate_rows),
                "matched_component_records": matched_count,
                "weather_observations": len(series),
            })
    return matched, coverage


def score_weather_only(test: list[dict]) -> list[tuple[float, dict]]:
    scored = [(float(row.get("weather_severity") or 0.0), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def score_component_weather(train: list[dict], test: list[dict]) -> list[tuple[float, dict]]:
    keys = ["component", "phase_bucket", "size", "aircraft_mass_class", "weather_stratum"]
    scores = train_scores(train, keys, TARGET, "rate", alpha=10.0)
    base_scores = train_scores(train, SCORE_SPECS[MAIN_SCORE]["keys"], TARGET, "rate", alpha=10.0)
    scored = []
    for row in test:
        score = scores.get(tuple(row.get(k, "") for k in keys))
        if score is None:
            score = base_scores.get(key_for(row, SCORE_SPECS[MAIN_SCORE]["keys"]), 0.0)
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def evaluate_weather(parts: list[dict], matched: list[dict], test_years: list[int], budgets: list[float]) -> tuple[list[dict], list[dict]]:
    yearly = []
    for test_year in test_years:
        train_all = [row for row in parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        train_weather = [row for row in matched if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in matched if int(row["year"]) == test_year]
        if not train_all or not test:
            continue
        scored_sets = {
            "component_transition_score": score_records(train_all, test, MAIN_SCORE, TARGET),
            "weather_severity_only": score_weather_only(test),
        }
        if train_weather:
            scored_sets["component_weather_transition_score"] = score_component_weather(train_weather, test)
        for score_name, scored in scored_sets.items():
            strata = ["all matched"]
            if score_name == "component_transition_score":
                strata.extend([
                    "normal weather",
                    "adverse weather",
                    "high wind",
                    "low visibility",
                    "precipitation",
                    "low ceiling",
                ])
            for stratum in strata:
                if stratum == "all matched":
                    scoped = scored
                elif stratum == "normal weather":
                    scoped = [(score, row) for score, row in scored if row["weather_stratum"] == "normal weather"]
                elif stratum == "adverse weather":
                    scoped = [(score, row) for score, row in scored if row["weather_stratum"] == "adverse weather"]
                elif stratum == "high wind":
                    scoped = [(score, row) for score, row in scored if row.get("high_wind")]
                elif stratum == "low visibility":
                    scoped = [(score, row) for score, row in scored if row.get("low_visibility")]
                elif stratum == "precipitation":
                    scoped = [(score, row) for score, row in scored if row.get("precipitation")]
                else:
                    scoped = [(score, row) for score, row in scored if row.get("low_ceiling")]
                if len(scoped) < 20 or sum(int(bool(row[TARGET])) for _, row in scoped) == 0:
                    continue
                for budget in budgets:
                    yearly.append({
                        "test_year": test_year,
                        "score": score_name,
                        "weather_stratum": stratum,
                        "budget_share": budget,
                        **selected_metrics(scoped, TARGET, budget),
                    })
    aggregate = aggregate_yearly(yearly, ["score", "weather_stratum", "budget_share"])
    return yearly, aggregate


def airport_coordinates(events: list[dict], airports: list[str]) -> dict[str, tuple[float, float]]:
    coords: dict[str, list[tuple[float, float]]] = defaultdict(list)
    airport_set = set(airports)
    for row in events:
        airport = normalized_airport(row.get("AIRPORT_ID"))
        if airport not in airport_set:
            continue
        try:
            lat = float(row.get("AIRPORT_LATITUDE"))
            lon = float(row.get("AIRPORT_LONGITUDE"))
        except (TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            coords[airport].append((lat, lon))
    out = {}
    for airport, values in coords.items():
        values = sorted(values)
        out[airport] = values[len(values) // 2]
    return out


def bbox_wkt(lat: float, lon: float, km: float = 50.0) -> str:
    lat_delta = km / 111.0
    lon_delta = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    west = lon - lon_delta
    east = lon + lon_delta
    south = lat - lat_delta
    north = lat + lat_delta
    return (
        f"POLYGON(({west:.5f} {south:.5f},{east:.5f} {south:.5f},"
        f"{east:.5f} {north:.5f},{west:.5f} {north:.5f},{west:.5f} {south:.5f}))"
    )


def load_gbif_cache() -> dict[tuple[str, int], dict]:
    if not GBIF_CACHE.exists():
        return {}
    rows = {}
    with GBIF_CACHE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows[(row["airport_id"], int(row["month"]))] = row
    return rows


def gbif_month_counts(airport: str, lat: float, lon: float, cache: dict[tuple[str, int], dict]) -> list[dict]:
    existing = [cache[(airport, month)] for month in range(1, 13) if (airport, month) in cache]
    if len(existing) == 12:
        return existing
    params = {
        "taxon_key": GBIF_AVES_TAXON_KEY,
        "geometry": bbox_wkt(lat, lon, 50.0),
        "eventDate": "2000-01-01,2025-12-31",
        "hasCoordinate": "true",
        "limit": "0",
        "facet": "month",
        "facetLimit": "12",
    }
    url = GBIF_OCCURRENCE_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return [
            {"airport_id": airport, "month": month, "gbif_bird_occurrences": 0, "gbif_query_ok": 0}
            for month in range(1, 13)
        ]
    counts = {int(item["name"]): int(item["count"]) for item in payload.get("facets", [{}])[0].get("counts", [])}
    rows = []
    for month in range(1, 13):
        rows.append({
            "airport_id": airport,
            "month": month,
            "gbif_bird_occurrences": counts.get(month, 0),
            "gbif_query_ok": 1,
        })
    time.sleep(0.12)
    return rows


def build_gbif_proxy(events: list[dict], airports: list[str]) -> list[dict]:
    coords = airport_coordinates(events, airports)
    cache = load_gbif_cache()
    rows_by_key = dict(cache)
    for airport in airports:
        if airport not in coords:
            continue
        for row in gbif_month_counts(airport, coords[airport][0], coords[airport][1], rows_by_key):
            rows_by_key[(airport, int(row["month"]))] = row
    rows = sorted(rows_by_key.values(), key=lambda r: (r["airport_id"], int(r["month"])))
    write_csv(GBIF_CACHE, rows)
    return [row for row in rows if row["airport_id"] in set(airports)]


def add_gbif_to_parts(parts: list[dict], gbif_rows: list[dict]) -> list[dict]:
    lookup = {
        (row["airport_id"], int(row["month"])): float(row["gbif_bird_occurrences"])
        for row in gbif_rows
    }
    by_airport = defaultdict(list)
    for (airport, month), value in lookup.items():
        by_airport[airport].append(value)
    medians = {airport: sorted(values)[len(values) // 2] for airport, values in by_airport.items() if values}
    out = []
    for row in parts:
        airport = normalized_airport(row.get("airport_id"))
        key = (airport, int(row["month"]))
        if key not in lookup:
            continue
        item = dict(row)
        value = lookup[key]
        item["airport_norm"] = airport
        item["gbif_bird_occurrences"] = value
        item["gbif_activity_stratum"] = "high bird activity" if value >= medians.get(airport, value + 1) else "low bird activity"
        out.append(item)
    return out


def score_gbif_only(test: list[dict]) -> list[tuple[float, dict]]:
    scored = [(float(row.get("gbif_bird_occurrences") or 0.0), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def evaluate_gbif(parts: list[dict], gbif_parts: list[dict], test_years: list[int], budgets: list[float]) -> tuple[list[dict], list[dict], list[dict]]:
    yearly = []
    coverage = []
    for test_year in test_years:
        train = [row for row in parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in gbif_parts if int(row["year"]) == test_year]
        if not train or not test:
            continue
        coverage.append({
            "test_year": test_year,
            "matched_component_records": len(test),
            "matched_damage_records": sum(int(bool(row[TARGET])) for row in test),
            "airports": len({row["airport_norm"] for row in test}),
        })
        scored_sets = {
            "component_transition_score": score_records(train, test, MAIN_SCORE, TARGET),
            "gbif_occurrence_only": score_gbif_only(test),
        }
        for score_name, scored in scored_sets.items():
            strata = ["all matched"]
            if score_name == "component_transition_score":
                strata.extend(["high bird activity", "low bird activity"])
            for stratum in strata:
                scoped = scored if stratum == "all matched" else [(score, row) for score, row in scored if row["gbif_activity_stratum"] == stratum]
                if len(scoped) < 50 or sum(int(bool(row[TARGET])) for _, row in scoped) == 0:
                    continue
                for budget in budgets:
                    yearly.append({
                        "test_year": test_year,
                        "score": score_name,
                        "gbif_stratum": stratum,
                        "budget_share": budget,
                        **selected_metrics(scoped, TARGET, budget),
                    })
    aggregate = aggregate_yearly(yearly, ["score", "gbif_stratum", "budget_share"])
    return yearly, aggregate, coverage


def run(smoke: bool, cache_only_weather: bool) -> None:
    out_dir = result_dir(smoke)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = load_events()
    parts = [row for row in component_rows(events) if 1990 <= int(row["year"]) <= 2025]
    airports = read_top_airports(2 if smoke else None)
    years_weather = [2024] if smoke else list(range(2000, 2026))
    years_eval = [2024] if smoke else list(range(1995, 2026))
    years_eval_2000 = [2024] if smoke else list(range(2000, 2026))
    budgets = [0.05] if smoke else [0.01, 0.025, 0.05, 0.10, 0.20]

    ntsb_summary, ntsb_examples = ntsb_stress_check(parts)
    ntsb_matched = ntsb_matched_stress_check(parts)
    write_csv(out_dir / "ntsb_nonwildlife_stress_check.csv", ntsb_summary)
    write_csv(out_dir / "ntsb_nonwildlife_stress_check_examples.csv", ntsb_examples)
    write_csv(out_dir / "ntsb_matched_stress_check.csv", ntsb_matched)

    frontier_yearly, frontier_aggregate = budget_frontier(parts, years_eval, budgets)
    write_csv(out_dir / "budget_frontier_yearly.csv", frontier_yearly)
    write_csv(out_dir / "budget_frontier_aggregate.csv", frontier_aggregate)

    bias_yearly, bias_aggregate = reporting_bias_strata(parts, years_eval, [0.05, 0.10])
    write_csv(out_dir / "reporting_bias_strata_yearly.csv", bias_yearly)
    write_csv(out_dir / "reporting_bias_strata_aggregate.csv", bias_aggregate)

    weather_matched, weather_coverage = weather_match_parts(parts, airports, years_weather, cache_only_weather)
    weather_yearly, weather_aggregate = evaluate_weather(parts, weather_matched, years_eval_2000, [0.05, 0.10])
    write_csv(out_dir / "noaa_weather_matched_coverage.csv", weather_coverage)
    write_csv(out_dir / "noaa_weather_yearly.csv", weather_yearly)
    write_csv(out_dir / "noaa_weather_aggregate.csv", weather_aggregate)

    gbif_airports = airports if smoke else read_top_airports(None)
    gbif_rows = build_gbif_proxy(events, gbif_airports)
    gbif_parts = add_gbif_to_parts(parts, gbif_rows)
    gbif_yearly, gbif_aggregate, gbif_coverage = evaluate_gbif(parts, gbif_parts, years_eval_2000, [0.05, 0.10])
    write_csv(out_dir / "gbif_airport_month_proxy.csv", gbif_rows)
    write_csv(out_dir / "gbif_ecological_proxy_yearly.csv", gbif_yearly)
    write_csv(out_dir / "gbif_ecological_proxy_aggregate.csv", gbif_aggregate)
    write_csv(out_dir / "gbif_ecological_proxy_coverage.csv", gbif_coverage)

    report_lines = [
        "# JSR priority experiments",
        "",
        "## NTSB non-wildlife stress check",
        pd.DataFrame(ntsb_summary).to_markdown(index=False),
        "",
        "## NTSB matched broad-parent stress check",
        pd.DataFrame(ntsb_matched).to_markdown(index=False),
        "",
        "## Capacity frontier",
        pd.DataFrame(frontier_aggregate).to_markdown(index=False),
        "",
        "## Reporting-bias strata",
        pd.DataFrame(bias_aggregate).to_markdown(index=False),
        "",
        "## ASOS/METAR weather",
        pd.DataFrame(weather_aggregate).to_markdown(index=False),
        "",
        "## GBIF ecological proxy",
        pd.DataFrame(gbif_aggregate).to_markdown(index=False),
    ]
    (out_dir / "priority_experiments_report.md").write_text("\n".join(report_lines), encoding="utf-8")
    print("\n".join(report_lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run priority validation experiments for the safety assurance allocation study.")
    parser.add_argument("--smoke", action="store_true", help="Run small checks before full experiments.")
    parser.add_argument("--cache-only-weather", action="store_true", help="Do not download new ASOS/METAR weather files.")
    args = parser.parse_args()
    run(args.smoke, args.cache_only_weather)


if __name__ == "__main__":
    main()
