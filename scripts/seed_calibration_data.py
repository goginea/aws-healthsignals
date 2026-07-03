#!/usr/bin/env python3
"""Seed Calibration Data — Backfill historical lag/severity data from Delphi API.

This script fetches 2-4 seasons of historical data from the CMU Delphi Epidata API,
calculates cross-correlation lags between metro counties and rural counties, and
populates the DynamoDB calibration table.

IMPORTANT API NOTES:
- NSSP signals use geo_type=county (NOT msa — msa returns empty)
- NSSP signals use time_type=week with epiweek format YYYYWW (NOT day/YYYYMMDD)
- County FIPS codes must be 5 digits (e.g., "48201" for Harris County)

Usage:
    python seed_calibration_data.py --seasons 3 --region us-east-1
    python seed_calibration_data.py --seasons 3 --dry-run  # Preview without writing

The calibration table is the foundation of HealthSignals predictions.
More seasons = higher confidence. Minimum 2 seasons recommended.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

http = urllib3.PoolManager()

# Delphi API configuration
DELPHI_API = "https://api.delphi.cmu.edu/epidata/covidcast/"

# Respiratory seasons in EPIWEEK format (YYYYWW)
# Respiratory season: Week 40 (early Oct) through Week 20 (mid May)
SEASONS = {
    "2022-23": {"start": "202239", "end": "202320"},
    "2023-24": {"start": "202339", "end": "202420"},
    "2024-25": {"start": "202439", "end": "202520"},
    "2025-26": {"start": "202539", "end": "202620"},
}

# Texas sentinel metros — represented by their PRIMARY county FIPS
# (NSSP doesn't support geo_type=msa, so we use the largest county in each MSA)
METRO_COUNTIES = {
    "26420": {  # Houston MSA
        "name": "Houston-The Woodlands-Sugar Land",
        "primary_county": "48201",  # Harris County (4.7M pop)
        "county_name": "Harris County",
    },
    "19100": {  # DFW MSA
        "name": "Dallas-Fort Worth-Arlington",
        "primary_county": "48113",  # Dallas County (2.6M pop)
        "county_name": "Dallas County",
    },
    "12420": {  # Austin MSA
        "name": "Austin-Round Rock-Georgetown",
        "primary_county": "48453",  # Travis County (1.3M pop)
        "county_name": "Travis County",
    },
    "41700": {  # San Antonio MSA
        "name": "San Antonio-New Braunfels",
        "primary_county": "48029",  # Bexar County (2.0M pop)
        "county_name": "Bexar County",
    },
}

# Rural counties to calibrate (grouped by nearest metro)
RURAL_COUNTIES = {
    "19100": [  # Near DFW
        {"fips": "48143", "name": "Erath County"},
        {"fips": "48251", "name": "Johnson County"},
        {"fips": "48367", "name": "Parker County"},
        {"fips": "48497", "name": "Wise County"},
    ],
    "26420": [  # Near Houston
        {"fips": "48287", "name": "Lee County"},
        {"fips": "48477", "name": "Washington County"},
        {"fips": "48039", "name": "Brazoria County"},
    ],
    "12420": [  # Near Austin
        {"fips": "48031", "name": "Blanco County"},
        {"fips": "48209", "name": "Hays County"},
        {"fips": "48055", "name": "Caldwell County"},
    ],
    "41700": [  # Near San Antonio
        {"fips": "48325", "name": "Medina County"},
        {"fips": "48163", "name": "Frio County"},
        {"fips": "48187", "name": "Guadalupe County"},
    ],
}

# Diseases to calibrate
DISEASES = {
    "influenza": "nssp:pct_ed_visits_influenza",
    "covid": "nssp:pct_ed_visits_covid",
    "rsv": "nssp:pct_ed_visits_rsv",
}


def fetch_time_series(signal: str, geo_value: str, start_week: str, end_week: str) -> list:
    """Fetch a weekly time series from Delphi API.

    Uses geo_type=county and time_type=week with epiweek format.
    """
    data_source, signal_name = signal.split(":")
    params = {
        "data_source": data_source,
        "signal": signal_name,
        "geo_type": "county",
        "geo_value": geo_value,
        "time_type": "week",
        "time_values": f"{start_week}-{end_week}",
    }
    url = f"{DELPHI_API}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

    response = http.request("GET", url, timeout=30.0)
    if response.status != 200:
        logger.warning(f"API returned {response.status} for {geo_value}/{signal_name}")
        return []

    data = json.loads(response.data.decode())
    if data.get("result") != 1:
        return []

    return data.get("epidata", [])


def find_peak(time_series: list) -> Optional[dict]:
    """Find the peak value and epiweek in a time series."""
    if not time_series:
        return None

    # Filter out null/zero values
    valid = [r for r in time_series if r.get("value") is not None and r["value"] > 0]
    if not valid:
        return None

    peak = max(valid, key=lambda x: x["value"])
    return {
        "week": peak["time_value"],  # Already in epiweek format (YYYYWW)
        "value": peak["value"],
    }


def calculate_lag_weeks(week1: int, week2: int) -> float:
    """Calculate lag in weeks between two epiweeks (YYYYWW integers).

    Handles year boundaries (e.g., 202452 → 202501 = 1 week).
    """
    year1, wk1 = divmod(week1, 100)
    year2, wk2 = divmod(week2, 100)

    # Convert to absolute week number
    abs_week1 = year1 * 52 + wk1
    abs_week2 = year2 * 52 + wk2

    return abs_week2 - abs_week1


def seed_calibration(num_seasons: int, region: str, dry_run: bool = False):
    """Main calibration seeding logic."""
    # Select most recent N seasons
    season_keys = list(SEASONS.keys())[-num_seasons:]
    logger.info(f"Seeding calibration for seasons: {season_keys}")

    if not dry_run:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table("healthsignals-calibration")
    else:
        table = None

    calibration_records = []

    for season_name in season_keys:
        season = SEASONS[season_name]
        logger.info(f"\n{'='*60}\nProcessing season: {season_name} (weeks {season['start']}-{season['end']})")

        for disease_name, signal in DISEASES.items():
            logger.info(f"\n  Disease: {disease_name} ({signal})")

            # Fetch metro (primary county) time series
            metro_peaks = {}
            for msa_code, metro_info in METRO_COUNTIES.items():
                county_fips = metro_info["primary_county"]
                ts = fetch_time_series(signal, county_fips, season["start"], season["end"])

                if ts:
                    peak = find_peak(ts)
                    if peak:
                        metro_peaks[msa_code] = peak
                        logger.info(
                            f"    {metro_info['name']} ({county_fips}): "
                            f"peak {peak['value']:.2f}% at week {peak['week']} "
                            f"({len(ts)} data points)"
                        )
                    else:
                        logger.warning(f"    {metro_info['name']}: data found but no peak > 0")
                else:
                    logger.warning(f"    {metro_info['name']} ({county_fips}): NO DATA")

            if not metro_peaks:
                logger.warning(f"  No metro peaks for {disease_name} {season_name} — skipping rural")
                continue

            # For each metro with a peak, calculate lag to rural counties
            for msa_code, peak in metro_peaks.items():
                rural_list = RURAL_COUNTIES.get(msa_code, [])
                for county in rural_list:
                    county_fips = county["fips"]
                    county_ts = fetch_time_series(
                        signal, county_fips, season["start"], season["end"]
                    )
                    county_peak = find_peak(county_ts)

                    if county_peak and county_peak["value"] > 0:
                        lag = calculate_lag_weeks(peak["week"], county_peak["week"])
                        severity_ratio = round(county_peak["value"] / peak["value"], 2) if peak["value"] > 0 else 1.0

                        record = {
                            "county_fips": county_fips,
                            "disease_season": f"{disease_name}_{season_name}",
                            "metro_msa_code": msa_code,
                            "metro_peak_week": str(peak["week"]),
                            "county_peak_week": str(county_peak["week"]),
                            "lag_weeks": Decimal(str(max(0, lag))),
                            "metro_peak_value": Decimal(str(round(peak["value"], 2))),
                            "county_peak_value": Decimal(str(round(county_peak["value"], 2))),
                            "severity_ratio": Decimal(str(max(0.1, severity_ratio))),
                            "season": season_name,
                            "calibrated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        calibration_records.append(record)
                        logger.info(
                            f"      {county['name']} ({county_fips}): "
                            f"lag={lag} weeks, severity={severity_ratio}×"
                        )
                    else:
                        logger.debug(
                            f"      {county['name']} ({county_fips}): "
                            f"no data or no peak"
                        )

    # Summary and write
    logger.info(f"\n{'='*60}")
    logger.info(f"Total calibration records: {len(calibration_records)}")

    if not calibration_records:
        logger.warning("No calibration records generated! Check API availability.")
        return

    if dry_run:
        logger.info("DRY RUN — not writing to DynamoDB")
        for r in calibration_records[:10]:
            logger.info(
                f"  {r['county_fips']} | {r['disease_season']} | "
                f"lag={r['lag_weeks']} | severity={r['severity_ratio']}×"
            )
        if len(calibration_records) > 10:
            logger.info(f"  ... and {len(calibration_records) - 10} more")
    else:
        logger.info("Writing to DynamoDB...")
        with table.batch_writer() as batch:
            for record in calibration_records:
                batch.put_item(Item=record)
        logger.info(f"Successfully wrote {len(calibration_records)} records")


def main():
    parser = argparse.ArgumentParser(description="Seed HealthSignals calibration data")
    parser.add_argument("--seasons", type=int, default=3, help="Number of seasons to backfill (2-4)")
    parser.add_argument("--region", type=str, default="us-east-1", help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DynamoDB")
    args = parser.parse_args()

    if args.seasons < 2 or args.seasons > 4:
        logger.error("Seasons must be 2-4")
        sys.exit(1)

    seed_calibration(args.seasons, args.region, args.dry_run)


if __name__ == "__main__":
    main()
