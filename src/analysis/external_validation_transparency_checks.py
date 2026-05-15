from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atads_exposure_validation import evaluate as evaluate_atads  # noqa: E402
from rolling_faa_wildlife_component_review import (  # noqa: E402
    SCORE_SPECS,
    key_for,
    score_records,
    train_scores,
)
from smoke_upgrade_validation import (  # noqa: E402
    COMPONENT_TERMS,
    HIER_SCORE,
    LARGE_TERMS,
    MEDIUM_TERMS,
    MAIN_SCORE,
    PHASE_PATTERNS,
    WILDLIFE_TERMS,
    WILDLIFE_REGEX,
    infer_mass_class,
    infer_component,
    infer_phase,
    infer_size,
    load_ntsb_wildlife_rows,
    load_parts,
    normalize_text,
    ntbs_connect,
    rows_from_recordset,
    selected_metrics,
    hierarchical_score_for,
    train_hierarchical_score_tables,
    train_hierarchical_scores,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "transparency_checks"

KEYS = SCORE_SPECS[MAIN_SCORE]["keys"]
TARGET = "part_damage"
EXTENDED_WILDLIFE_TERMS = sorted(set(
    WILDLIFE_TERMS + ["duck", "eagle", "turkey", "elk", "moose", "waterfowl"]
))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def format_terms(terms: list[str], max_items: int = 12) -> str:
    shown = terms[:max_items]
    suffix = "" if len(terms) <= max_items else "; ..."
    return "; ".join(shown) + suffix


def dictionary_rows() -> list[dict]:
    rows = [
        {
            "dictionary": "Wildlife extraction",
            "target": "wildlife-related NTSB record",
            "terms": format_terms(EXTENDED_WILDLIFE_TERMS, 24),
            "rule": "A record enters the external set when any term appears in narrative or finding text.",
        },
        {
            "dictionary": "Large animal-size mapping",
            "target": "large",
            "terms": format_terms(LARGE_TERMS),
            "rule": "Large terms override medium terms when both are present.",
        },
        {
            "dictionary": "Medium animal-size mapping",
            "target": "medium",
            "terms": format_terms(MEDIUM_TERMS),
            "rule": "Medium terms are used when no large term is present.",
        },
    ]
    for component, terms in COMPONENT_TERMS.items():
        rows.append({
            "dictionary": "Component mapping",
            "target": component.replace("_", " "),
            "terms": format_terms(terms),
            "rule": "Engine is assigned first when engine terms are present; otherwise the first component hit is used.",
        })
    for phase, terms in PHASE_PATTERNS:
        rows.append({
            "dictionary": "Phase mapping",
            "target": phase,
            "terms": format_terms(terms),
            "rule": "The first phase bucket with a matched term is used.",
        })
    rows.append({
        "dictionary": "Aircraft mass class",
        "target": "mass class",
        "terms": "certificated maximum gross weight",
        "rule": "Weight in pounds is converted to kilograms and mapped to FAA mass classes 1--5.",
    })
    return rows


def component_mapping_rows() -> list[dict]:
    rows = []
    for component, terms in COMPONENT_TERMS.items():
        priority = "first" if component == "engine" else "after engine priority"
        rows.append({
            "component_family": component.replace("_", " "),
            "terms": format_terms(terms),
            "priority": priority,
        })
    rows.append({
        "component_family": "residual component",
        "terms": "No listed component-family term is present.",
        "priority": "all-mapped sensitivity",
    })
    return rows


def ntsb_sample_rows(ntsb_rows: list[dict], n: int = 30) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in ntsb_rows:
        grouped[(row.get("component", ""), row.get("phase_bucket", ""))].append(row)
    candidates = []
    for group_rows in grouped.values():
        group_rows = sorted(
            group_rows,
            key=lambda r: (-int(bool(r.get("severe"))), int(r.get("year") or 0), str(r.get("ev_id", ""))),
        )
        candidates.append(group_rows[0])
    remaining = [r for r in ntsb_rows if r not in candidates]
    sorted_rows = sorted(
        candidates,
        key=lambda r: (str(r.get("component", "")), str(r.get("phase_bucket", "")), int(r.get("year") or 0)),
    )
    sorted_rows.extend(sorted(
        remaining,
        key=lambda r: (
            -int(bool(r.get("severe"))),
            str(r.get("component", "")),
            str(r.get("phase_bucket", "")),
            int(r.get("year") or 0),
            str(r.get("ev_id", "")),
        ),
    ))
    out = []
    for row in sorted_rows[:n]:
        out.append({
            "ev_id": row.get("ev_id", ""),
            "year": row.get("year", ""),
            "damage": row.get("damage", ""),
            "severe": "yes" if row.get("severe") else "no",
            "component": str(row.get("component", "")).replace("_", " "),
            "phase": row.get("phase_bucket", ""),
            "size": row.get("size", ""),
            "mass": row.get("aircraft_mass_class", ""),
            "engine_text": "yes" if row.get("engine_text") else "no",
        })
    return out


def first_hit(blob: str, terms: list[str]) -> str:
    hits = [term for term in terms if term in blob]
    return "; ".join(hits[:4])


def context_snippet(blob: str, terms: list[str], width: int = 180) -> str:
    positions = [blob.find(term) for term in terms if term in blob]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return blob[:width].strip()
    center = min(positions)
    start = max(0, center - width // 2)
    end = min(len(blob), start + width)
    snippet = " ".join(blob[start:end].split())
    return snippet


def component_group(component: str) -> str:
    if component == "engine":
        return "engine"
    if component == "wing_rotor":
        return "wing-rotor"
    if component == "windshield":
        return "windshield"
    if component == "other":
        return "residual component"
    return "other named component"


def load_ntsb_audit_candidates(start_year: int = 1990, end_year: int = 2025) -> list[dict]:
    conn = ntbs_connect()
    terms = EXTENDED_WILDLIFE_TERMS
    term_clauses = []
    for term in terms:
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
        component = infer_component(blob)
        phase = infer_phase(blob)
        size = infer_size(blob)
        damage = str(item.get("damage") or "").upper()
        severe = damage in {"SUBS", "DEST"} or int(item.get("inj_tot_f") or 0) > 0 or int(item.get("inj_tot_s") or 0) > 0
        component_terms = COMPONENT_TERMS.get(component, [])
        component_hit = first_hit(blob, component_terms)
        wildlife_hit = first_hit(blob, terms)
        phase_terms = []
        for phase_name, terms_for_phase in PHASE_PATTERNS:
            if phase_name == phase:
                phase_terms = terms_for_phase
                break
        size_terms = LARGE_TERMS if size == "LARGE" else MEDIUM_TERMS if size == "MEDIUM" else []
        component_consistent = bool(component_hit) or component == "other"
        phase_consistent = bool(first_hit(blob, phase_terms)) or phase == "unknown"
        size_consistent = bool(first_hit(blob, size_terms)) or size == "UNKNOWN"
        severe_consistent = severe == (damage in {"SUBS", "DEST"} or int(item.get("inj_tot_f") or 0) > 0 or int(item.get("inj_tot_s") or 0) > 0)
        mapped_consistent = bool(wildlife_hit) and component_consistent and phase_consistent and size_consistent and severe_consistent
        evidence_terms = component_terms + phase_terms + size_terms + terms
        out.append({
            "ev_id": item.get("ev_id"),
            "year": int(item.get("ev_year") or 0),
            "damage": damage,
            "severe": "yes" if severe else "no",
            "component_group": component_group(component),
            "component": component.replace("_", " "),
            "phase": phase,
            "size": size,
            "mass": infer_mass_class(item.get("cert_max_gr_wt")),
            "wildlife_hit": wildlife_hit,
            "component_hit": component_hit if component_hit else "none",
            "phase_hit": first_hit(blob, phase_terms) if phase_terms else "none",
            "size_hit": first_hit(blob, size_terms) if size_terms else "none",
            "component_consistent": int(component_consistent),
            "phase_consistent": int(phase_consistent),
            "size_consistent": int(size_consistent),
            "severe_consistent": int(severe_consistent),
            "mapped_consistent": int(mapped_consistent),
            "evidence_snippet": context_snippet(blob, evidence_terms),
        })
    return sorted(out, key=lambda r: (r["component_group"], -int(r["severe"] == "yes"), r["year"], str(r["ev_id"])))


def stratified_audit_rows(ntsb_rows: list[dict], n: int = 100) -> list[dict]:
    groups = ["engine", "wing-rotor", "windshield", "other named component", "residual component"]
    quota = n // len(groups)
    candidates = load_ntsb_audit_candidates(1990, 2025)
    selected = []
    selected_ids = set()
    for group in groups:
        subset = [r for r in candidates if r["component_group"] == group]
        take = subset[:quota]
        selected.extend(take)
        selected_ids.update((r["ev_id"], r["component"], r["phase"], r["year"]) for r in take)
    if len(selected) < n:
        for row in candidates:
            key = (row["ev_id"], row["component"], row["phase"], row["year"])
            if key in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(key)
            if len(selected) == n:
                break
    return selected[:n]


def audit_summary_rows(audit_rows: list[dict]) -> list[dict]:
    out = []
    groups = ["engine", "wing-rotor", "windshield", "other named component", "residual component", "all"]
    for group in groups:
        subset = audit_rows if group == "all" else [r for r in audit_rows if r["component_group"] == group]
        if not subset:
            continue
        out.append({
            "component_group": group,
            "records_checked": len(subset),
            "wildlife_evidence_rate": sum(int(bool(r["wildlife_hit"])) for r in subset) / len(subset),
            "component_term_support": sum(int(r["component_hit"] != "none") for r in subset) / len(subset),
            "phase_agreement": sum(int(r["phase_consistent"]) for r in subset) / len(subset),
            "size_agreement": sum(int(r["size_consistent"]) for r in subset) / len(subset),
            "severity_rule_agreement": sum(int(r["severe_consistent"]) for r in subset) / len(subset),
            "overall_mapping_agreement": sum(int(r["mapped_consistent"]) for r in subset) / len(subset),
            "direct_text_eligible": sum(int(r["component_hit"] != "none") for r in subset) / len(subset),
        })
    return out


def score_ntsb_rows(parts: list[dict], ntsb_rows: list[dict]) -> tuple[list[dict], dict[float, dict]]:
    train = [r for r in parts if 2016 <= int(r["year"]) <= 2025]
    tables, global_rate = train_hierarchical_score_tables(train, TARGET)
    nw_scores = sorted([hierarchical_score_for(row, tables, global_rate) for row in train], reverse=True)
    scored = []
    for row in ntsb_rows:
        item = dict(row)
        item["score"] = hierarchical_score_for(item, tables, global_rate)
        scored.append(item)

    budgets = {}
    for share in [0.05, 0.10]:
        k = max(1, math.ceil(len(nw_scores) * share))
        cutoff = nw_scores[k - 1]
        nw_top = sum(1 for value in nw_scores if value >= cutoff)
        budgets[share] = {"cutoff": cutoff, "nw_top": nw_top, "nw_total": len(nw_scores)}
    return scored, budgets


def candidate_to_validation_row(row: dict) -> dict:
    item = dict(row)
    item["component"] = str(item.get("component", "")).replace(" ", "_")
    item["phase_bucket"] = item.pop("phase")
    item["aircraft_mass_class"] = item.pop("mass")
    severe = item.get("severe")
    item["severe"] = severe == "yes" if isinstance(severe, str) else bool(severe)
    item["text_supported_component"] = int(item.get("component_hit") != "none")
    item["engine_text"] = int(item["component"] == "engine" and item.get("component_hit") != "none")
    return item


def ntsb_enrichment_sets(candidates: list[dict]) -> dict[str, list[dict]]:
    all_rows = [candidate_to_validation_row(row) for row in candidates]
    text_supported = [row for row in all_rows if int(row.get("text_supported_component", 0)) == 1]
    return {
        "text-supported component records": text_supported,
        "all mapped records": all_rows,
    }


def ntsb_enrichment_summary_rows(scored: list[dict], budgets: dict[float, dict], enrichment_set: str) -> list[dict]:
    severe = [r for r in scored if r.get("severe")]
    rows = []
    for share, meta in budgets.items():
        cutoff = meta["cutoff"]
        nw_top = int(meta["nw_top"])
        nw_total = int(meta["nw_total"])
        top = [r for r in scored if r["score"] >= cutoff]
        top_severe = [r for r in severe if r["score"] >= cutoff]
        rows.append({
            "enrichment_set": enrichment_set,
            "budget_share": share,
            "records": len(scored),
            "top_records": len(top),
            "top_record_share": len(top) / len(scored) if scored else 0.0,
            "severe_records": len(severe),
            "top_severe_records": len(top_severe),
            "top_severe_capture_rate": len(top_severe) / len(severe) if severe else 0.0,
            "enrichment_odds_ratio": odds_ratio_counts(len(top), len(scored), nw_top, nw_total) if scored else 0.0,
        })
    return rows


def odds_ratio_counts(ext_top: int, ext_total: int, nw_top: int, nw_total: int) -> float:
    ext_rest = ext_total - ext_top
    nw_rest = nw_total - nw_top
    return ((ext_top + 0.5) * (nw_rest + 0.5)) / ((ext_rest + 0.5) * (nw_top + 0.5))


def ntsb_bootstrap_rows(scored: list[dict], budgets: dict[float, dict], reps: int, seed: int, enrichment_set: str) -> list[dict]:
    rng = random.Random(seed)
    severe = [r for r in scored if r.get("severe")]
    rows = []
    for share, meta in budgets.items():
        cutoff = meta["cutoff"]
        nw_top = int(meta["nw_top"])
        nw_total = int(meta["nw_total"])
        observed_top = sum(1 for r in severe if r["score"] >= cutoff)
        observed_ext_top = sum(1 for r in scored if r["score"] >= cutoff)
        observed_capture = observed_top / len(severe) if severe else 0.0
        observed_or = odds_ratio_counts(observed_ext_top, len(scored), nw_top, nw_total) if scored else 0.0
        cap_samples = []
        or_samples = []
        for _ in range(reps):
            severe_sample = [severe[rng.randrange(len(severe))] for _ in range(len(severe))]
            all_sample = [scored[rng.randrange(len(scored))] for _ in range(len(scored))]
            severe_top = sum(1 for r in severe_sample if r["score"] >= cutoff)
            ext_top = sum(1 for r in all_sample if r["score"] >= cutoff)
            cap_samples.append(severe_top / len(severe_sample))
            or_samples.append(odds_ratio_counts(ext_top, len(all_sample), nw_top, nw_total))
        cap_samples.sort()
        or_samples.sort()
        low_idx = int(0.025 * (reps - 1))
        high_idx = int(0.975 * (reps - 1))
        rows.append({
            "enrichment_set": enrichment_set,
            "budget_share": share,
            "records": len(scored),
            "severe_records": len(severe),
            "top_severe_records": observed_top,
            "capture_rate": observed_capture,
            "capture_ci_low": cap_samples[low_idx],
            "capture_ci_high": cap_samples[high_idx],
            "odds_ratio": observed_or,
            "or_ci_low": or_samples[low_idx],
            "or_ci_high": or_samples[high_idx],
        })
    return rows


def score_hierarchical_alpha(train: list[dict], test: list[dict], alpha: float) -> list[tuple[float, dict]]:
    tables, global_rate = train_hierarchical_score_tables(train, TARGET, alpha=alpha)
    scored = [(hierarchical_score_for(row, tables, global_rate), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def aggregate_metric_rows(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "test_years": set(),
        "test_component_records": 0,
        "target_records": 0,
        "selected_component_records": 0,
        "captured_target_records": 0,
    })
    for row in rows:
        key = tuple(row[k] for k in ["alpha", "budget_share"])
        item = groups[key]
        item["test_years"].add(row["test_year"])
        for field in ["test_component_records", "target_records", "selected_component_records", "captured_target_records"]:
            item[field] += row[field]
    out = []
    for (alpha, budget), item in groups.items():
        selected_rate = item["captured_target_records"] / item["selected_component_records"]
        overall_rate = item["target_records"] / item["test_component_records"]
        out.append({
            "alpha": alpha,
            "budget_share": budget,
            "test_years": len(item["test_years"]),
            "test_component_records": item["test_component_records"],
            "target_records": item["target_records"],
            "selected_component_records": item["selected_component_records"],
            "captured_target_records": item["captured_target_records"],
            "capture_rate": item["captured_target_records"] / item["target_records"],
            "hit_rate": selected_rate,
            "lift": selected_rate / overall_rate,
        })
    return sorted(out, key=lambda r: (r["budget_share"], r["alpha"]))


def alpha_sensitivity_rows(parts: list[dict], test_years: list[int], alphas: list[float]) -> list[dict]:
    rows = []
    for test_year in test_years:
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [r for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue
        for alpha in alphas:
            scored = score_hierarchical_alpha(train, test, alpha)
            for budget in [0.05, 0.10]:
                metrics = selected_metrics(scored, TARGET, budget)
                rows.append({
                    "test_year": test_year,
                    "alpha": alpha,
                    "budget_share": budget,
                    "test_component_records": len(test),
                    **metrics,
                })
    return aggregate_metric_rows(rows)


def train_cell_counts(train: list[dict]) -> dict[tuple, int]:
    counts: dict[tuple, int] = defaultdict(int)
    for row in train:
        counts[key_for(row, KEYS)] += 1
    return counts


def sparse_band(count: int) -> str:
    if count <= 10:
        return "1--10"
    if count <= 50:
        return "11--50"
    if count <= 200:
        return "51--200"
    return ">200"


def band_metrics(selected: list[dict], test: list[dict], counts: dict[tuple, int]) -> dict[str, dict]:
    selected_keys = {(row["event_id"], row["component"]) for row in selected}
    groups: dict[str, dict] = defaultdict(lambda: {"units": 0, "damage": 0, "selected": 0, "captured": 0})
    for row in test:
        band = sparse_band(counts.get(key_for(row, KEYS), 0))
        item = groups[band]
        item["units"] += 1
        item["damage"] += int(bool(row[TARGET]))
        if (row["event_id"], row["component"]) in selected_keys:
            item["selected"] += 1
            item["captured"] += int(bool(row[TARGET]))
    return groups


def sparse_cell_rows(parts: list[dict], test_years: list[int]) -> list[dict]:
    aggregate: dict[tuple, dict] = defaultdict(lambda: {"units": 0, "damage": 0, "selected": 0, "captured": 0})
    for test_year in test_years:
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [r for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue
        counts = train_cell_counts(train)
        variants = {
            "hierarchical": score_hierarchical_alpha(train, test, 10.0),
            "direct smoothed": score_records(train, test, MAIN_SCORE, TARGET),
        }
        for rule, scored in variants.items():
            for budget in [0.05, 0.10]:
                k = max(1, math.ceil(len(scored) * budget))
                selected = [row for _, row in scored[:k]]
                for band, metrics in band_metrics(selected, test, counts).items():
                    key = (rule, budget, band)
                    item = aggregate[key]
                    for field in item:
                        item[field] += metrics[field]
    out = []
    band_order = {"1--10": 0, "11--50": 1, "51--200": 2, ">200": 3}
    for (rule, budget, band), item in aggregate.items():
        hit = item["captured"] / item["selected"] if item["selected"] else 0.0
        overall = item["damage"] / item["units"] if item["units"] else 0.0
        out.append({
            "rule": rule,
            "budget_share": budget,
            "training_cell_count": band,
            "test_component_records": item["units"],
            "target_records": item["damage"],
            "selected_component_records": item["selected"],
            "captured_target_records": item["captured"],
            "capture_rate": item["captured"] / item["damage"] if item["damage"] else 0.0,
            "hit_rate": hit,
            "lift": hit / overall if overall else 0.0,
        })
    return sorted(out, key=lambda r: (r["budget_share"], band_order.get(r["training_cell_count"], 99), r["rule"]))


def clipped_probability(value: float) -> float:
    return min(1.0 - 1e-6, max(1e-6, float(value)))


def probability_diagnostic_rows(parts: list[dict], test_years: list[int]) -> list[dict]:
    aggregate: dict[tuple, dict] = defaultdict(lambda: {
        "records": 0,
        "damage": 0,
        "pred_sum": 0.0,
        "brier": 0.0,
        "log_loss": 0.0,
    })
    for test_year in test_years:
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [r for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue
        direct_scores = train_scores(train, KEYS, TARGET, "rate")
        hier_tables, hier_global = train_hierarchical_score_tables(train, TARGET)
        counts = train_cell_counts(train)
        for row in test:
            y = int(bool(row[TARGET]))
            band = sparse_band(counts.get(key_for(row, KEYS), 0))
            probabilities = {
                "direct smoothed": direct_scores.get(key_for(row, KEYS), 0.0),
                "hierarchical fallback": hierarchical_score_for(row, hier_tables, hier_global),
            }
            for rule, prob in probabilities.items():
                p = clipped_probability(prob)
                for group in [band, "all"]:
                    item = aggregate[(rule, group)]
                    item["records"] += 1
                    item["damage"] += y
                    item["pred_sum"] += p
                    item["brier"] += (p - y) ** 2
                    item["log_loss"] += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    order = {"1--10": 0, "11--50": 1, "51--200": 2, ">200": 3, "all": 4}
    out = []
    for (rule, band), item in aggregate.items():
        observed = item["damage"] / item["records"]
        predicted = item["pred_sum"] / item["records"]
        out.append({
            "rule": rule,
            "training_cell_count": band,
            "records": item["records"],
            "damage_records": item["damage"],
            "observed_rate": observed,
            "mean_predicted_rate": predicted,
            "calibration_error": abs(predicted - observed),
            "brier_score": item["brier"] / item["records"],
            "log_loss": item["log_loss"] / item["records"],
        })
    return sorted(out, key=lambda r: (order.get(r["training_cell_count"], 99), r["rule"]))


def fallback_coverage_rows(parts: list[dict], test_years: list[int]) -> list[dict]:
    aggregate: dict[str, dict] = defaultdict(lambda: {"records": 0, "damage": 0})
    levels = [
        ("four-level cell", KEYS),
        ("component-phase-size", KEYS[:3]),
        ("component-phase", KEYS[:2]),
        ("component", KEYS[:1]),
        ("global", []),
    ]
    for test_year in test_years:
        train = [r for r in parts if test_year - 5 <= int(r["year"]) <= test_year - 1]
        test = [r for r in parts if int(r["year"]) == test_year]
        if not train or not test:
            continue
        train_keys = {}
        for label, cols in levels[:-1]:
            train_keys[label] = {key_for(row, cols) for row in train}
        for row in test:
            assigned = "global"
            for label, cols in levels[:-1]:
                if key_for(row, cols) in train_keys[label]:
                    assigned = label
                    break
            aggregate[assigned]["records"] += 1
            aggregate[assigned]["damage"] += int(bool(row[TARGET]))
    out = []
    total = sum(v["records"] for v in aggregate.values())
    for label, _ in levels:
        item = aggregate.get(label, {"records": 0, "damage": 0})
        out.append({
            "fallback_level": label,
            "records": item["records"],
            "record_share": item["records"] / total if total else 0.0,
            "damage_records": item["damage"],
            "damage_rate": item["damage"] / item["records"] if item["records"] else 0.0,
        })
    return out


def atads_ci_rows(reps: int, seed: int) -> tuple[list[dict], list[dict]]:
    yearly, coverage = evaluate_atads()
    rng = random.Random(seed)
    rows = []
    for budget in sorted({row["budget_share"] for row in yearly}):
        subset = [row for row in yearly if row["budget_share"] == budget]
        observed = pooled_atads(subset)
        lift_samples = []
        weighted_samples = []
        capture_samples = []
        weighted_capture_samples = []
        for _ in range(reps):
            sample = [subset[rng.randrange(len(subset))] for _ in range(len(subset))]
            pooled = pooled_atads(sample)
            lift_samples.append(pooled["lift"])
            weighted_samples.append(pooled["weighted_lift"])
            capture_samples.append(pooled["capture_rate"])
            weighted_capture_samples.append(pooled["weighted_capture_rate"])
        lift_samples.sort()
        weighted_samples.sort()
        capture_samples.sort()
        weighted_capture_samples.sort()
        low_idx = int(0.025 * (reps - 1))
        high_idx = int(0.975 * (reps - 1))
        rows.append({
            "budget_share": budget,
            "test_years": len(subset),
            **observed,
            "capture_ci_low": capture_samples[low_idx],
            "capture_ci_high": capture_samples[high_idx],
            "weighted_capture_ci_low": weighted_capture_samples[low_idx],
            "weighted_capture_ci_high": weighted_capture_samples[high_idx],
            "lift_ci_low": lift_samples[low_idx],
            "lift_ci_high": lift_samples[high_idx],
            "weighted_lift_ci_low": weighted_samples[low_idx],
            "weighted_lift_ci_high": weighted_samples[high_idx],
        })
    return rows, coverage


def pooled_atads(rows: list[dict]) -> dict:
    selected = sum(row["selected_component_records"] for row in rows)
    captured = sum(row["captured_target_records"] for row in rows)
    total = sum(row["target_records"] for row in rows)
    test = sum(row["test_component_records"] for row in rows)
    selected_rate = captured / selected
    overall_rate = total / test
    selected_weighted_damage = sum(row.get("selected_weighted_damage", 0.0) for row in rows)
    total_weighted_damage = sum(row.get("total_weighted_damage", 0.0) for row in rows)
    return {
        "test_component_records": test,
        "target_records": total,
        "selected_component_records": selected,
        "captured_target_records": captured,
        "capture_rate": captured / total,
        "hit_rate": selected_rate,
        "lift": selected_rate / overall_rate,
        "weighted_capture_rate": selected_weighted_damage / total_weighted_damage if total_weighted_damage else 0.0,
        "weighted_lift": sum(row["weighted_lift"] for row in rows) / len(rows),
    }


def run(smoke: bool, reps: int) -> None:
    seed = 20260512
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    parts = load_parts()
    test_years = [2024, 2025] if smoke else list(range(1995, 2026))
    alphas = [1.0, 10.0] if smoke else [1.0, 5.0, 10.0, 25.0, 50.0]
    ntsb_candidates = load_ntsb_audit_candidates(1990, 2025)
    enrichment_sets = ntsb_enrichment_sets(ntsb_candidates)
    ntsb_enrichment = []
    ntsb_ci = []
    strict_rows = enrichment_sets["text-supported component records"]
    for set_name, set_rows in enrichment_sets.items():
        scored_ntsb, budget_meta = score_ntsb_rows(parts, set_rows)
        ntsb_enrichment.extend(ntsb_enrichment_summary_rows(scored_ntsb, budget_meta, set_name))
        ntsb_ci.extend(ntsb_bootstrap_rows(scored_ntsb, budget_meta, reps, seed, set_name))

    dictionary = dictionary_rows()
    component_map = component_mapping_rows()
    examples = ntsb_sample_rows(strict_rows, 10 if smoke else 20)
    audit_records = stratified_audit_rows(strict_rows, 20 if smoke else 100)
    audit_summary = audit_summary_rows(audit_records)
    alpha = alpha_sensitivity_rows(parts, test_years, alphas)
    sparse = sparse_cell_rows(parts, test_years)
    diagnostics = probability_diagnostic_rows(parts, test_years)
    fallback = fallback_coverage_rows(parts, test_years)
    atads_ci, coverage = atads_ci_rows(reps, seed)

    write_csv(RESULT_DIR / "ntsb_dictionary.csv", dictionary)
    write_csv(RESULT_DIR / "ntsb_component_mapping.csv", component_map)
    write_csv(RESULT_DIR / "ntsb_sample_mapped_records.csv", examples)
    write_csv(RESULT_DIR / "ntsb_stratified_audit_records.csv", audit_records)
    write_csv(RESULT_DIR / "ntsb_stratified_audit_summary.csv", audit_summary)
    write_csv(RESULT_DIR / "ntsb_external_enrichment_sets.csv", ntsb_enrichment)
    write_csv(RESULT_DIR / "ntsb_bootstrap_ci.csv", ntsb_ci)
    write_csv(RESULT_DIR / "hierarchical_alpha_sensitivity.csv", alpha)
    write_csv(RESULT_DIR / "sparse_cell_performance.csv", sparse)
    write_csv(RESULT_DIR / "probability_diagnostics.csv", diagnostics)
    write_csv(RESULT_DIR / "hierarchical_fallback_coverage.csv", fallback)
    write_csv(RESULT_DIR / "atads_bootstrap_ci.csv", atads_ci)

    print("NTSB bootstrap")
    print(pd.DataFrame(ntsb_ci).to_string(index=False))
    print("\nAlpha sensitivity")
    print(pd.DataFrame(alpha).to_string(index=False))
    print("\nSparse-cell performance")
    print(pd.DataFrame(sparse).to_string(index=False))
    print("\nProbability diagnostics")
    print(pd.DataFrame(diagnostics).to_string(index=False))
    print("\nHierarchical fallback coverage")
    print(pd.DataFrame(fallback).to_string(index=False))
    print("\nATADS bootstrap")
    print(pd.DataFrame(atads_ci).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build external validation transparency checks.")
    parser.add_argument("--smoke", action="store_true", help="Run a small check before full tables.")
    parser.add_argument("--reps", type=int, default=500, help="Bootstrap repetitions.")
    args = parser.parse_args()
    run(args.smoke, args.reps)


if __name__ == "__main__":
    main()

