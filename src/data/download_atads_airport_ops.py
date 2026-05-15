from __future__ import annotations

import argparse
import re
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "atads"
META_DIR = PROJECT_ROOT / "data" / "metadata" / "download_logs"
ATADS_FORM = "https://www.aspm.faa.gov/opsnet/sys/airport.asp"
ATADS_SERVER = "https://www.aspm.faa.gov/opsnet/sys/opsnet-server-x.asp"

CALC_FIELDS = ",".join(
    [
        "SUM(IFR_ITIN_AC) AS IFR_ITIN_AC",
        "SUM(IFR_ITIN_AT) AS IFR_ITIN_AT",
        "SUM(IFR_ITIN_GA) AS IFR_ITIN_GA",
        "SUM(IFR_ITIN_MI) AS IFR_ITIN_MI",
        "SUM(IFR_ITIN_AC+IFR_ITIN_AT+IFR_ITIN_GA+IFR_ITIN_MI) AS TOT_ITII",
        "SUM(VFR_ITIN_AC) AS VFR_ITIN_AC",
        "SUM(VFR_ITIN_AT) AS VFR_ITIN_AT",
        "SUM(VFR_ITIN_GA) AS VFR_ITIN_GA",
        "SUM(VFR_ITIN_MI) AS VFR_ITIN_MI",
        "SUM(VFR_ITIN_AC+VFR_ITIN_AT+VFR_ITIN_GA+VFR_ITIN_MI) AS TOT_ITIV",
        "SUM(AC) AS AC",
        "SUM(ATAXI) AS ATAXI",
        "SUM(IFR_ITIN_GA+VFR_ITIN_GA) AS GA",
        "SUM(IFR_ITIN_MI+VFR_ITIN_MI) AS MIL",
        "SUM(AC+ATAXI+IFR_ITIN_GA+VFR_ITIN_GA+IFR_ITIN_MI+VFR_ITIN_MI) AS TOT_ITI",
        "SUM(LOCAL_GA) AS LOCAL_GA",
        "SUM(LOCAL_MIL) AS LOCAL_MIL",
        "SUM(LOCAL_GA+LOCAL_MIL) AS TOT_LOC",
        "SUM(TOTAL) AS TOTAL",
    ]
)


def airport_suffix(airport: str | None) -> str:
    if not airport:
        return ""
    codes = [code.strip().upper() for code in re.split(r"[,\s]+", airport) if code.strip()]
    if len(codes) == 1:
        return f"_{codes[0]}"
    return "_SELECTED"


def year_request_body(year: int, airport: str | None) -> dict[str, str]:
    start = f"{year}01"
    end = f"{year}12"
    where = f"YYYYMM>={start} AND YYYYMM<={end}"
    llist = ""
    if airport:
        llist = f"'{airport.upper()}'"
        where += f" AND LOCID IN ({llist})"
    line = (
        f"SELECT LOCID,YYYYMM,{CALC_FIELDS} FROM TOWER_DAY "
        f"WHERE {where} GROUP BY LOCID,YYYYMM ORDER BY LOCID,YYYYMM"
    )
    return {
        "dstyle": "m",
        "dfld": "yyyymm",
        "dlist": "",
        "fromdate": start,
        "todate": end,
        "llist": llist,
        "keylist": "LOCID,YYYYMM",
        "line": line,
        "cmd": "air_bas",
        "nopage": "y",
        "nost": "y",
        "defs": "",
        "avgdays": "1",
        "oktosave": "y",
        "addifr": "",
        "addvfr": "",
        "additi": "y",
        "addloc": "y",
        "reptype": "bas",
        "reportformat": "asp",
        "facilityType": "l",
        "ftype": "0",
        "iti": "1",
        "loc": "1",
    }


def parse_atads_html(html: str, year: int) -> pd.DataFrame:
    tables = pd.read_html(StringIO(html))
    if not tables:
        return pd.DataFrame()
    df = tables[0].iloc[:, :11].copy()
    df.columns = [
        "locid",
        "date_label",
        "air_carrier",
        "air_taxi",
        "general_aviation",
        "military",
        "itinerant_total",
        "local_civil",
        "local_military",
        "local_total",
        "total_operations",
    ]
    df = df[df["locid"].notna()].copy()
    df["locid"] = df["locid"].astype(str).str.strip().str.upper()
    df = df[df["locid"].ne("TOTAL:")]
    df = df[df["date_label"].notna()].copy()
    df["date_label"] = df["date_label"].astype(str).str.strip()
    date_parts = df["date_label"].str.extract(r"^(?P<month>\d{2})/(?P<year>\d{4})$")
    df["year"] = pd.to_numeric(date_parts["year"], errors="coerce")
    df["month"] = pd.to_numeric(date_parts["month"], errors="coerce")
    df = df[df["year"].eq(year) & df["month"].between(1, 12)].copy()
    numeric_cols = [
        "air_carrier",
        "air_taxi",
        "general_aviation",
        "military",
        "itinerant_total",
        "local_civil",
        "local_military",
        "local_total",
        "total_operations",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    return df[["locid", "year", "month", *numeric_cols]]


def fetch_year(session: requests.Session, year: int, airport: str | None, save_html: bool) -> pd.DataFrame:
    body = year_request_body(year, airport)
    response = session.post(
        ATADS_SERVER,
        data=body,
        headers={"Referer": ATADS_FORM},
        timeout=90,
    )
    response.raise_for_status()
    html = response.text
    if save_html:
        suffix = airport_suffix(airport)
        (RAW_DIR / f"atads_airport_ops_{year}{suffix}.html").write_text(html, encoding="utf-8")
    return parse_atads_html(html, year)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download FAA ATADS airport monthly operations.")
    parser.add_argument("--start-year", type=int, default=1990)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--airport", type=str, default=None, help="Optional ATADS three-letter facility code for smoke tests.")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--parse-html-dir", type=str, default=None, help="Parse previously downloaded ATADS HTML files from this directory.")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)

    if args.parse_html_dir:
        html_dir = Path(args.parse_html_dir)
        frames = []
        log_rows = []
        for year in range(args.start_year, args.end_year + 1):
            suffix = airport_suffix(args.airport)
            path = html_dir / f"atads_airport_ops_{year}{suffix}.html"
            status = "ok"
            row_count = 0
            try:
                html = path.read_text(encoding="utf-8")
                frame = parse_atads_html(html, year)
                row_count = len(frame)
                frames.append(frame)
            except Exception as exc:  # noqa: BLE001
                status = f"error: {exc}"
            log_rows.append({"year": year, "airport": args.airport or "ALL", "rows": row_count, "status": status, "seconds": 0})
            print(f"{year}: {status} ({row_count} rows)")
        if frames:
            out = pd.concat(frames, ignore_index=True)
            label = airport_suffix(args.airport)
            out_path = RAW_DIR / f"atads_airport_month_ops_{args.start_year}_{args.end_year}{label}.csv"
            out.to_csv(out_path, index=False, encoding="utf-8-sig")
            print(f"wrote {out_path} ({len(out):,} rows)")
        log_path = META_DIR / f"atads_airport_ops_parse_log_{args.start_year}_{args.end_year}.csv"
        pd.DataFrame(log_rows).to_csv(log_path, index=False, encoding="utf-8-sig")
        print(f"wrote {log_path}")
        return

    session = requests.Session()
    session.get(ATADS_FORM, timeout=60).raise_for_status()

    frames = []
    log_rows = []
    for year in range(args.start_year, args.end_year + 1):
        start_time = time.time()
        status = "ok"
        row_count = 0
        try:
            frame = fetch_year(session, year, args.airport, args.save_html)
            row_count = len(frame)
            frames.append(frame)
        except Exception as exc:  # noqa: BLE001
            status = f"error: {exc}"
        log_rows.append({"year": year, "airport": args.airport or "ALL", "rows": row_count, "status": status, "seconds": round(time.time() - start_time, 2)})
        print(f"{year}: {status} ({row_count} rows)")
        time.sleep(args.sleep)

    if frames:
        out = pd.concat(frames, ignore_index=True)
        label = airport_suffix(args.airport)
        out_path = RAW_DIR / f"atads_airport_month_ops_{args.start_year}_{args.end_year}{label}.csv"
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"wrote {out_path} ({len(out):,} rows)")

    log_path = META_DIR / f"atads_airport_ops_download_log_{args.start_year}_{args.end_year}.csv"
    pd.DataFrame(log_rows).to_csv(log_path, index=False, encoding="utf-8-sig")
    print(f"wrote {log_path}")


if __name__ == "__main__":
    main()
