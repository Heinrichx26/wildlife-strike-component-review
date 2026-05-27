from __future__ import annotations

import argparse
import bisect
import csv
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atads_exposure_validation import normalized_airport  # noqa: E402
from rolling_faa_wildlife_component_review import SCORE_SPECS, key_for, score_records, train_scores  # noqa: E402
from jsr_priority_experiments import aggregate_yearly, selected_metrics, write_csv  # noqa: E402
from jsr_weather_smoke import parse_incident_datetime  # noqa: E402
from smoke_faa_wildlife import enrich, load_rows, text  # noqa: E402
from wildlife_component_data import component_rows  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOP_AIRPORTS = PROJECT_ROOT / "data" / "metadata" / "top_nwsd_airports_120.txt"
ASOS_DIR = PROJECT_ROOT / "data" / "raw" / "noaa_asos_metar"
TARGET = "part_damage"
MAIN_SCORE = "component_phase_size_mass_rate"
IEM_URL = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
    "station={station}&data=tmpf&data=sknt&data=vsby&data=skyc1&data=skyl1&data=p01i"
    "&year1={year}&month1=1&day1=1&year2={year}&month2=12&day2=31"
    "&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=3"
)


def result_dir(smoke: bool) -> Path:
    base = PROJECT_ROOT / "results" / ("smoke_tests" if smoke else "experiments")
    return base / "jsr_priority"


def read_top_airports(limit: int | None = None) -> list[str]:
    airports = [item.strip().upper() for item in TOP_AIRPORTS.read_text(encoding="utf-8").split(",") if item.strip()]
    return airports[:limit] if limit else airports


def load_parts() -> list[dict]:
    events = [enrich(row) for row in load_rows()]
    return [row for row in component_rows(events) if 1990 <= int(row["year"]) <= 2025]


def asos_path(airport: str, year: int) -> Path:
    return ASOS_DIR / str(year) / f"{airport}.csv"


def download_asos_year(airport: str, year: int, timeout: int = 45, refresh: bool = False) -> dict:
    path = asos_path(airport, year)
    if path.exists() and path.stat().st_size > 80 and not refresh:
        return {"airport_id": airport, "year": year, "status": "cached", "bytes": path.stat().st_size}
    path.parent.mkdir(parents=True, exist_ok=True)
    url = IEM_URL.format(station=airport, year=year)
    tmp = path.with_suffix(".tmp")
    last_error = "failed"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = response.read()
            if b"station,valid" not in data[:100]:
                raise RuntimeError("unexpected response")
            tmp.write_bytes(data)
            tmp.replace(path)
            return {"airport_id": airport, "year": year, "status": "downloaded", "bytes": path.stat().st_size}
        except Exception as exc:
            last_error = f"failed:{type(exc).__name__}"
            if tmp.exists():
                tmp.unlink()
            time.sleep(4.0 * (attempt + 1))
    return {"airport_id": airport, "year": year, "status": last_error, "bytes": 0}


def download_all(airports: list[str], years: list[int], workers: int, refresh: bool) -> list[dict]:
    tasks = [(airport, year) for airport in airports for year in years]
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(download_asos_year, airport, year, refresh=refresh) for airport, year in tasks]
        for future in as_completed(futures):
            rows.append(future.result())
    return sorted(rows, key=lambda r: (r["airport_id"], r["year"]))


def parse_number(value: object) -> float | None:
    s = text(value)
    if s in {"", "M"}:
        return None
    if s == "T":
        return 0.01
    try:
        return float(s)
    except ValueError:
        return None


def parse_asos_file(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size <= 80:
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            try:
                stamp = datetime.strptime(raw["valid"], "%Y-%m-%d %H:%M")
            except Exception:
                continue
            sknt = parse_number(raw.get("sknt"))
            vsby = parse_number(raw.get("vsby"))
            skyl = parse_number(raw.get("skyl1"))
            p01i = parse_number(raw.get("p01i"))
            tmpf = parse_number(raw.get("tmpf"))
            wind_mps = sknt * 0.514444 if sknt is not None else None
            visibility_m = vsby * 1609.344 if vsby is not None else None
            ceiling_m = skyl * 0.3048 if skyl is not None else None
            precip_mm = p01i * 25.4 if p01i is not None else 0.0
            temperature_c = (tmpf - 32.0) * 5.0 / 9.0 if tmpf is not None else None
            severity = 0.0
            if wind_mps is not None:
                severity += min(wind_mps / 18.0, 1.0)
            if visibility_m is not None:
                severity += max(0.0, min((10000.0 - visibility_m) / 10000.0, 1.0))
            if ceiling_m is not None:
                severity += max(0.0, min((1500.0 - ceiling_m) / 1500.0, 1.0))
            if precip_mm and precip_mm > 0:
                severity += 0.5
            adverse = int(
                (wind_mps is not None and wind_mps >= 10.0)
                or (visibility_m is not None and visibility_m < 5000.0)
                or (ceiling_m is not None and ceiling_m < 1000.0)
                or (precip_mm is not None and precip_mm > 0.0)
            )
            rows.append({
                "timestamp": stamp,
                "wind_speed_mps": wind_mps,
                "visibility_m": visibility_m,
                "ceiling_m": ceiling_m,
                "temperature_c": temperature_c,
                "precip_mm": precip_mm,
                "weather_severity": severity,
                "adverse_weather": adverse,
            })
    rows.sort(key=lambda row: row["timestamp"])
    return rows


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


def match_weather(parts: list[dict], airports: list[str], years: list[int]) -> tuple[list[dict], list[dict]]:
    airport_set = set(airports)
    years_set = set(years)
    parts_by_key: dict[tuple[str, int], list[dict]] = {}
    for row in parts:
        airport = normalized_airport(row.get("airport_id"))
        year = int(row["year"])
        if airport not in airport_set or year not in years_set:
            continue
        parts_by_key.setdefault((airport, year), []).append(row)

    matched = []
    coverage = []
    for airport in airports:
        for year in years:
            candidate_rows = parts_by_key.get((airport, year), [])
            series = parse_asos_file(asos_path(airport, year))
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


def evaluate(parts: list[dict], matched: list[dict], test_years: list[int]) -> tuple[list[dict], list[dict]]:
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
                strata.extend(["normal weather", "adverse weather", "high wind", "low visibility", "precipitation", "low ceiling"])
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
                if len(scoped) < 30 or sum(int(bool(row[TARGET])) for _, row in scoped) == 0:
                    continue
                for budget in [0.05, 0.10]:
                    yearly.append({
                        "test_year": test_year,
                        "score": score_name,
                        "weather_stratum": stratum,
                        "budget_share": budget,
                        **selected_metrics(scoped, TARGET, budget),
                    })
    aggregate = aggregate_yearly(yearly, ["score", "weather_stratum", "budget_share"])
    return yearly, aggregate


def run(smoke: bool, workers: int, refresh: bool, skip_download: bool, airport_limit: int | None) -> None:
    out_dir = result_dir(smoke)
    out_dir.mkdir(parents=True, exist_ok=True)
    airports = read_top_airports(2 if smoke else airport_limit)
    years = [2024] if smoke else list(range(2000, 2026))
    parts = load_parts()
    if skip_download:
        download_log = [
            {
                "airport_id": airport,
                "year": year,
                "status": "cached" if asos_path(airport, year).exists() else "missing",
                "bytes": asos_path(airport, year).stat().st_size if asos_path(airport, year).exists() else 0,
            }
            for airport in airports for year in years
        ]
    else:
        download_log = download_all(airports, years, workers, refresh)
    matched, coverage = match_weather(parts, airports, years)
    test_years = [2024] if smoke else list(range(2000, 2026))
    yearly, aggregate = evaluate(parts, matched, test_years)
    write_csv(out_dir / "asos_weather_download_log.csv", download_log)
    write_csv(out_dir / "asos_weather_coverage.csv", coverage)
    write_csv(out_dir / "asos_weather_yearly.csv", yearly)
    write_csv(out_dir / "asos_weather_aggregate.csv", aggregate)
    print(pd.DataFrame(aggregate).to_string(index=False))
    print(pd.DataFrame(download_log).groupby("status").size().to_string())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full ASOS/METAR weather validation for the review-allocation study.")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--airport-limit", type=int, default=None)
    args = parser.parse_args()
    run(args.smoke, args.workers, args.refresh, args.skip_download, args.airport_limit)


if __name__ == "__main__":
    main()
