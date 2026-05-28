from __future__ import annotations

import bisect
import csv
import math
import sys
import argparse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolling_faa_wildlife_component_review import score_records  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "results" / "experiments" / "field_validation"
NOAA_DIR = PROJECT_ROOT / "data" / "raw" / "noaa_global_hourly"
ISD_HISTORY = PROJECT_ROOT / "data" / "raw" / "noaa_isd_history.csv"
ISD_HISTORY_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
NOAA_ACCESS_URL = "https://www.ncei.noaa.gov/data/global-hourly/access/{year}/{station}.csv"
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


def ensure_isd_history() -> None:
    if ISD_HISTORY.exists():
        return
    ISD_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(ISD_HISTORY_URL, ISD_HISTORY)


def station_lookup() -> dict[str, str]:
    ensure_isd_history()
    df = pd.read_csv(ISD_HISTORY, dtype=str)
    df = df[(df["ICAO"].fillna("") != "") & (df["USAF"].fillna("") != "999999")]
    df["END_SORT"] = pd.to_numeric(df["END"], errors="coerce").fillna(0)
    lookup = {}
    for icao, group in df.groupby("ICAO"):
        selected = group.sort_values("END_SORT").iloc[-1]
        lookup[str(icao).upper()] = f"{selected['USAF']}{selected['WBAN']}"
    return lookup


def load_events() -> list[dict]:
    return [enrich(row) for row in load_rows()]


def top_airports(events: list[dict], years: set[int], limit: int) -> list[str]:
    counts = Counter()
    for row in events:
        if int(row["_YEAR"]) not in years:
            continue
        airport = text(row.get("AIRPORT_ID")).upper()
        if len(airport) == 4 and airport.startswith("K"):
            counts[airport] += 1
    return [airport for airport, _ in counts.most_common(limit)]


def weather_path(station: str, year: int) -> Path:
    return NOAA_DIR / str(year) / f"{station}.csv"


def download_weather(station: str, year: int, timeout_seconds: int = 90, cache_only: bool = False) -> Path | None:
    path = weather_path(station, year)
    if path.exists() and path.stat().st_size > 0:
        return path
    if path.exists() and path.stat().st_size == 0:
        path.unlink()
    if cache_only:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    url = NOAA_ACCESS_URL.format(year=year, station=station)
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            with path.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception:
        if path.exists():
            path.unlink()
        return None
    return path if path.exists() and path.stat().st_size > 0 else None


def encoded_number(value: str, missing: int, scale: float) -> float | None:
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    if abs(number) == missing:
        return None
    return number / scale


def parse_wind(value: str) -> float | None:
    parts = str(value or "").split(",")
    if len(parts) < 4:
        return None
    return encoded_number(parts[3], 9999, 10.0)


def parse_visibility(value: str) -> float | None:
    parts = str(value or "").split(",")
    if not parts:
        return None
    return encoded_number(parts[0], 999999, 1.0)


def parse_ceiling(value: str) -> float | None:
    parts = str(value or "").split(",")
    if not parts:
        return None
    return encoded_number(parts[0], 99999, 1.0)


def parse_temperature(value: str) -> float | None:
    parts = str(value or "").split(",")
    if not parts:
        return None
    return encoded_number(parts[0], 9999, 10.0)


def parse_precip(row: dict) -> float:
    total = 0.0
    for key, value in row.items():
        if not key.startswith("AA") or not value:
            continue
        parts = str(value).split(",")
        if len(parts) < 2:
            continue
        depth = encoded_number(parts[1], 9999, 10.0)
        if depth and depth > 0:
            total += depth
    return total


def weather_severity(obs: dict) -> float:
    wind = obs.get("wind_speed_mps")
    visibility = obs.get("visibility_m")
    ceiling = obs.get("ceiling_m")
    precip = obs.get("precip_mm")
    score = 0.0
    if wind is not None:
        score += min(float(wind) / 18.0, 1.0)
    if visibility is not None:
        score += max(0.0, min((10000.0 - float(visibility)) / 10000.0, 1.0))
    if ceiling is not None:
        score += max(0.0, min((1500.0 - float(ceiling)) / 1500.0, 1.0))
    if precip is not None and float(precip) > 0:
        score += 0.5
    return score


def load_weather_series(station: str, year: int, cache_only: bool = False) -> list[dict]:
    path = download_weather(station, year, cache_only=cache_only)
    if not path:
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            call_sign = text(raw.get("CALL_SIGN")).upper()
            report_type = text(raw.get("REPORT_TYPE")).upper()
            if report_type not in {"FM-15", "FM-16", "FM-12"}:
                continue
            try:
                stamp = datetime.fromisoformat(text(raw.get("DATE")))
            except ValueError:
                continue
            obs = {
                "station": station,
                "timestamp": stamp,
                "call_sign": call_sign,
                "wind_speed_mps": parse_wind(raw.get("WND", "")),
                "visibility_m": parse_visibility(raw.get("VIS", "")),
                "ceiling_m": parse_ceiling(raw.get("CIG", "")),
                "temperature_c": parse_temperature(raw.get("TMP", "")),
                "precip_mm": parse_precip(raw),
            }
            obs["weather_severity"] = weather_severity(obs)
            obs["adverse_weather"] = int(
                (obs["wind_speed_mps"] is not None and obs["wind_speed_mps"] >= 10.0)
                or (obs["visibility_m"] is not None and obs["visibility_m"] < 5000.0)
                or (obs["ceiling_m"] is not None and obs["ceiling_m"] < 1000.0)
                or (obs["precip_mm"] is not None and obs["precip_mm"] > 0.0)
            )
            rows.append(obs)
    rows.sort(key=lambda row: row["timestamp"])
    if rows and rows[-1]["timestamp"] < datetime(year, 12, 25):
        return []
    return rows


def parse_incident_datetime(row: dict) -> datetime | None:
    date_text = text(row.get("incident_date"))
    time_text = text(row.get("incident_time"))
    if not date_text or not time_text:
        return None
    try:
        hour, minute = [int(part) for part in time_text.split(":")[:2]]
        date = datetime.fromisoformat(date_text[:10])
        return date.replace(hour=hour, minute=minute)
    except Exception:
        return None


def nearest_weather(stamps: list[datetime], rows: list[dict], stamp: datetime) -> tuple[dict | None, float | None]:
    index = bisect.bisect_left(stamps, stamp)
    candidates = []
    if index < len(rows):
        candidates.append(rows[index])
    if index > 0:
        candidates.append(rows[index - 1])
    if not candidates:
        return None, None
    selected = min(candidates, key=lambda row: abs(row["timestamp"] - stamp))
    minutes = abs(selected["timestamp"] - stamp).total_seconds() / 60.0
    if minutes > 120:
        return None, minutes
    return selected, minutes


def build_matched_parts(limit_airports: int = 8, years: set[int] | None = None, cache_only: bool = False) -> tuple[list[dict], list[dict]]:
    years = years or {2024, 2025}
    events = load_events()
    airports = top_airports(events, years, limit_airports)
    stations = station_lookup()
    airport_station = {airport: stations.get(airport) for airport in airports if stations.get(airport)}
    weather_cache = {}
    for airport, station in airport_station.items():
        for year in years:
            series = load_weather_series(station, year, cache_only=cache_only)
            weather_cache[(airport, year)] = {
                "rows": series,
                "stamps": [row["timestamp"] for row in series],
            }

    parts = []
    for row in component_rows(events):
        year = int(row["year"])
        airport = text(row["airport_id"]).upper()
        if year not in years or airport not in airport_station:
            continue
        incident_stamp = parse_incident_datetime(row)
        if not incident_stamp:
            continue
        cache = weather_cache.get((airport, year), {"rows": [], "stamps": []})
        obs, minutes = nearest_weather(cache["stamps"], cache["rows"], incident_stamp)
        if not obs:
            continue
        item = dict(row)
        item["weather_station"] = airport_station[airport]
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
        parts.append(item)

    coverage = []
    for airport in airports:
        for year in sorted(years):
            available = len(weather_cache.get((airport, year), {"rows": []})["rows"])
            coverage.append({
                "airport_id": airport,
                "year": year,
                "station": airport_station.get(airport, ""),
                "weather_observations": available,
            })
    return parts, coverage


def metric(selected: list[dict], population: list[dict], score: str, budget: float) -> dict:
    total = sum(int(bool(row[TARGET])) for row in population)
    captured = sum(int(bool(row[TARGET])) for row in selected)
    selected_rate = captured / len(selected) if selected else 0.0
    overall_rate = total / len(population) if population else 0.0
    return {
        "score": score,
        "budget_share": budget,
        "test_component_records": len(population),
        "target_records": total,
        "selected_component_records": len(selected),
        "captured_target_records": captured,
        "capture_rate": captured / total if total else 0.0,
        "selected_target_rate": selected_rate,
        "overall_target_rate": overall_rate,
        "lift": selected_rate / overall_rate if overall_rate else 0.0,
    }


def score_weather_only(test: list[dict]) -> list[tuple[float, dict]]:
    scored = [(float(row.get("weather_severity") or 0.0), row) for row in test]
    scored.sort(key=lambda x: (-x[0], x[1]["event_id"], x[1]["component"]))
    return scored


def evaluate_weather(parts: list[dict]) -> tuple[list[dict], list[dict]]:
    all_events = [enrich(row) for row in load_rows()]
    all_parts = [row for row in component_rows(all_events) if 2019 <= int(row["year"]) <= 2025]
    yearly = []
    for test_year in [2024, 2025]:
        train = [row for row in all_parts if test_year - 5 <= int(row["year"]) <= test_year - 1]
        test = [row for row in parts if int(row["year"]) == test_year]
        if not train or not test:
            continue
        scored_component = score_records(train, test, MAIN_SCORE, TARGET)
        scored_weather = score_weather_only(test)
        scored_sets = {
            "component_transition_score": scored_component,
            "weather_severity_only": scored_weather,
        }
        for score_name, scored in scored_sets.items():
            strata = ["all matched"]
            if score_name == "component_transition_score":
                strata.extend(["normal weather", "adverse weather"])
            for stratum in strata:
                if stratum == "all matched":
                    scoped = scored
                else:
                    scoped = [(score, row) for score, row in scored if row["weather_stratum"] == stratum]
                population = [row for _, row in scoped]
                if not population:
                    continue
                for budget in [0.05, 0.10]:
                    selected_count = max(1, math.ceil(len(scoped) * budget))
                    selected = [row for _, row in scoped[:selected_count]]
                    yearly.append({
                        "test_year": test_year,
                        "weather_stratum": stratum,
                        **metric(selected, population, score_name, budget),
                    })
    return yearly, aggregate(yearly)


def aggregate(rows: list[dict]) -> list[dict]:
    out = []
    keys = sorted({(row["score"], row["weather_stratum"], row["budget_share"]) for row in rows})
    for score_name, stratum, budget in keys:
        subset = [row for row in rows if row["score"] == score_name and row["weather_stratum"] == stratum and row["budget_share"] == budget]
        selected = sum(row["selected_component_records"] for row in subset)
        captured = sum(row["captured_target_records"] for row in subset)
        target = sum(row["target_records"] for row in subset)
        total = sum(row["test_component_records"] for row in subset)
        selected_rate = captured / selected if selected else 0.0
        overall_rate = target / total if total else 0.0
        out.append({
            "score": score_name,
            "weather_stratum": stratum,
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
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test NOAA Global Hourly weather matching for NWSD component records.")
    parser.add_argument("--airport-limit", type=int, default=8)
    parser.add_argument("--years", nargs="+", type=int, default=[2024, 2025])
    parser.add_argument("--cache-only", action="store_true", help="Use already downloaded NOAA files and skip network downloads.")
    args = parser.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    matched, coverage = build_matched_parts(limit_airports=args.airport_limit, years=set(args.years), cache_only=args.cache_only)
    yearly, aggregate_rows = evaluate_weather(matched)
    write_csv(RESULT_DIR / "weather_smoke_matched_component_records.csv", matched[:1000])
    write_csv(RESULT_DIR / "weather_smoke_coverage.csv", coverage)
    write_csv(RESULT_DIR / "weather_smoke_yearly.csv", yearly)
    write_csv(RESULT_DIR / "weather_smoke_aggregate.csv", aggregate_rows)
    print(pd.DataFrame(aggregate_rows).to_string(index=False))
    print(pd.DataFrame(coverage).to_string(index=False))


if __name__ == "__main__":
    main()

