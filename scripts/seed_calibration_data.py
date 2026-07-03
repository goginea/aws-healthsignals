#!/usr/bin/env python3
"""Seed Calibration Data — Backfill historical lag/severity data from Delphi API.

This script fetches 2-4 seasons of historical data from the CMU Delphi Epidata API,
calculates cross-correlation lags between metros and counties, and populates the
DynamoDB calibration table.

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
from datetime import datetime
from decimal import Decimal
from typing import Optional

import boto3
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

http = urllib3.PoolManager()

# Delphi API configuration
DELPHI_API = "https://api.delphi.cmu.edu/epidata/covidcast/"

# Respiratory seasons to backfill (start year of each season)
SEASONS = {
    "2022-23": {"start": "20220901", "end": "20230601"},
    "2023-24": {"start": "20230901", "end": "20240601"},
    "2024-25": {"start": "20240901", "end": "20250601"},
    "2025-26": {"start": "20250901", "end": "20260601"},
}

# Texas sentinel metros
METROS = ["26420", "19100", "12420", "41700"]

# Diseases to calibrate
DISEASES = {
    "influenza": "nssp:pct_ed_visits_influenza",
    "covid": "nssp:pct_ed_visits_covid",
    "rsv": "nssp:pct_ed_visits_rsv",
}


def fetch_time_series(signal: str, geo_type: str, geo_value: str, start: str, end: str) -> list:
    """Fetch a time series from Delphi API."""
    data_source, signal_name = signal.split(":")
    params = {
        "data_source": data_source,
        "signal": signal_name,
        "geo_type": geo_type,
        "geo_value": geo_value,
        "time_type": "day",
        "time_values": f"{start}-{end}",
    }
    url = f"{DELPHI_API}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

    response = http.request("GET", url, timeout=30.0)
    if response.status != 200:
        logger.warning(f"API returned {response.status} for {geo_value}/{signal_name}")
        return []

    data = json.loads(response.data.decode())
    return data.get("epidata", [])


def find_peak(time_series: list) -> Optional[dict]:
    """Find the peak value and date in a time series."""
    if not time_series:
        return None

    peak = max(time_series, key=lambda x: x.get("value", 0))
    return {
        "date": peak["time_value"],
        "value": peak["value"],
        "week": date_to_epiweek(peak["time_value"]),
    }


def date_to_epiweek(date_int: int) -> str:
    """Convert YYYYMMDD integer to YYYYWW epiweek string."""
    date_str = str(date_int)
    dt = datetime.strptime(date_str, "%Y%m%d")
    year = dt.year
    week = dt.isocalendar()[1]
    return f"{year}{week:02d}"


def calculate_lag_weeks(date1: int, date2: int) -> float:
    """Calculate lag in weeks between two YYYYMMDD dates."""
    dt1 = datetime.strptime(str(date1), "%Y%m%d")
    dt2 = datetime.strptime(str(date2), "%Y%m%d")
    delta = dt2 - dt1
    return round(delta.days / 7.0, 1)


def seed_calibration(num_seasons: int, region: str, dry_run: bool = False):
    """Main calibration seeding logic."""
    # Select seasons to process
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
        logger.info(f"\n{'='*60}\nProcessing season: {season_name}")

        for disease_name, signal in DISEASES.items():
            logger.info(f"\n  Disease: {disease_name} ({signal})")

            # Fetch metro time series
            metro_peaks = {}
            for msa_code in METROS:
                ts = fetch_time_series(signal, "msa", msa_code, season["start"], season["end"])
                peak = find_peak(ts)
                if peak:
                    metro_peaks[msa_code] = peak
                    logger.info(f"    Metro {msa_code}: peak {peak['value']:.2f}% on {peak['date']}")
                else:
                    logger.warning(f"    Metro {msa_code}: NO DATA for {disease_name} {season_name}")

            # For each metro with a peak, calculate lag to sample counties
            # In production, this would iterate over all subscribed counties
            # For seeding, we use counties near each metro
            sample_counties = {
                "19100": ["48143", "48251", "48367"],  # Counties near DFW
                "26420": ["48287", "48477", "48039"],  # Counties near Houston
                "12420": ["48031", "48209", "48491"],  # Counties near Austin
                "41700": ["48325", "48163", "48187"],  # Counties near San Antonio
            }

            for msa_code, peak in metro_peaks.items():
                counties = sample_counties.get(msa_code, [])
                for county_fips in counties:
                    # Try to get county-level data
                    county_ts = fetch_time_series(
                        signal, "county", county_fips, season["start"], season["end"]
                    )
                    county_peak = find_peak(county_ts)

                    if county_peak and county_peak["value"] > 0:
                        lag = calculate_lag_weeks(peak["date"], county_peak["date"])
                        severity_ratio = round(county_peak["value"] / peak["value"], 2) if peak["value"] > 0 else 1.0

                        record = {
                            "metro_county_pair": f"{msa_code}_{county_fips}",
                            "disease_season": f"{disease_name}_{season_name}",
                            "metro_peak_week": peak["week"],
                            "county_peak_week": county_peak["week"],
                            "lag_weeks": Decimal(str(max(0, lag))),
                            "metro_peak_value": Decimal(str(round(peak["value"], 2))),
                            "county_peak_value": Decimal(str(round(county_peak["value"], 2))),
                            "severity_ratio": Decimal(str(max(0.1, severity_ratio))),
                            "season": season_name,
                            "calibrated_at": datetime.utcnow().isoformat(),
                        }
                        calibration_records.append(record)
                        logger.info(
                            f"      {county_fips}: lag={lag}wk, severity={severity_ratio}×"
                        )
                    else:
                        logger.debug(f"      {county_fips}: No county-level data available")

    # Write to DynamoDB
    logger.info(f"\n{'='*60}")
    logger.info(f"Total calibration records: {len(calibration_records)}")

    if dry_run:
        logger.info("DRY RUN — not writing to DynamoDB")
        for r in calibration_records[:5]:
            logger.info(f"  Sample: {r['metro_county_pair']} | {r['disease_season']} | lag={r['lag_weeks']}")
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
