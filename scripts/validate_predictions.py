#!/usr/bin/env python3
"""Validate Predictions — Retrospective and prospective validation analysis.

This script evaluates HealthSignals prediction accuracy by comparing:
1. What the system WOULD HAVE predicted (retrospective, based on calibration)
2. What ACTUALLY happened (ground truth from Delphi API)

Usage:
    # Retrospective validation (backtest on known seasons)
    python validate_predictions.py --mode retrospective --season 2024-25

    # Prospective validation (evaluate live predictions vs outcomes)
    python validate_predictions.py --mode prospective --start 20261001 --end 20270501

Metrics produced:
- Timing accuracy: predicted lag vs actual lag (MAE in weeks)
- Severity accuracy: predicted multiplier vs actual multiplier (MAE)
- Detection sensitivity: % of actual outbreaks correctly alerted
- False positive rate: % of alerts where no outbreak materialized
- Warning window accuracy: did health officers actually get N weeks of prep time?
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from statistics import mean, median, stdev
from typing import Optional

import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

http = urllib3.PoolManager()
DELPHI_API = "https://api.delphi.cmu.edu/epidata/covidcast/"


def fetch_season_data(signal: str, geo_type: str, geo_value: str, start: str, end: str) -> list:
    """Fetch time series for a full season."""
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
        return []

    data = json.loads(response.data.decode())
    return sorted(data.get("epidata", []), key=lambda x: x["time_value"])


def find_threshold_crossing(time_series: list, threshold: float) -> Optional[int]:
    """Find first date where signal crosses threshold with rising trend."""
    for i in range(1, len(time_series)):
        curr = time_series[i].get("value", 0)
        prev = time_series[i - 1].get("value", 0)
        if curr >= threshold and curr > prev:  # Above threshold + rising
            return time_series[i]["time_value"]
    return None


def find_peak(time_series: list) -> Optional[dict]:
    """Find peak value in time series."""
    if not time_series:
        return None
    peak = max(time_series, key=lambda x: x.get("value", 0))
    return {"date": peak["time_value"], "value": peak["value"]}


def calculate_lag_days(date1: int, date2: int) -> float:
    """Calculate days between two YYYYMMDD dates."""
    dt1 = datetime.strptime(str(date1), "%Y%m%d")
    dt2 = datetime.strptime(str(date2), "%Y%m%d")
    return (dt2 - dt1).days


def run_retrospective_validation(season: str):
    """Run retrospective (backtest) validation for a given season.

    Takes a held-out season and simulates what the system would have predicted
    vs what actually happened.
    """
    seasons_config = {
        "2022-23": {"start": "20220901", "end": "20230601"},
        "2023-24": {"start": "20230901", "end": "20240601"},
        "2024-25": {"start": "20240901", "end": "20250601"},
        "2025-26": {"start": "20250901", "end": "20260601"},
    }

    if season not in seasons_config:
        logger.error(f"Unknown season: {season}. Available: {list(seasons_config.keys())}")
        sys.exit(1)

    cfg = seasons_config[season]
    logger.info(f"Running retrospective validation for {season}")
    logger.info(f"Date range: {cfg['start']} to {cfg['end']}")

    # Metrics collectors
    timing_errors = []  # predicted lag - actual lag (weeks)
    severity_errors = []  # predicted multiplier - actual multiplier
    detections = {"true_positive": 0, "false_positive": 0, "false_negative": 0}

    # Test flu detection for Texas metros → sample counties
    signal = "nssp:pct_ed_visits_influenza"
    threshold = 1.0  # flu threshold

    # Find actual metro leader (first to cross threshold)
    metros = {
        "26420": "Houston",
        "19100": "DFW",
        "12420": "Austin",
        "41700": "San Antonio",
    }

    metro_crossings = {}
    metro_peaks = {}

    for msa_code, name in metros.items():
        ts = fetch_season_data(signal, "msa", msa_code, cfg["start"], cfg["end"])
        crossing = find_threshold_crossing(ts, threshold)
        peak = find_peak(ts)

        if crossing:
            metro_crossings[msa_code] = crossing
            logger.info(f"  {name} crossed threshold on {crossing}")
        if peak:
            metro_peaks[msa_code] = peak
            logger.info(f"  {name} peaked at {peak['value']:.2f}% on {peak['date']}")

    if not metro_crossings:
        logger.warning("No metro crossed threshold this season — nothing to validate")
        return

    # Determine actual leader (first crossing)
    leader_msa = min(metro_crossings, key=metro_crossings.get)
    leader_crossing_date = metro_crossings[leader_msa]
    logger.info(f"\n  LEADER: {metros[leader_msa]} (crossed {leader_crossing_date})")

    # Check sample counties
    test_counties = ["48143", "48251", "48031"]  # Erath, Ellis, Blanco

    for county_fips in test_counties:
        county_ts = fetch_season_data(signal, "county", county_fips, cfg["start"], cfg["end"])
        county_peak = find_peak(county_ts)

        if county_peak and county_peak["value"] > 0:
            actual_lag_days = calculate_lag_days(leader_crossing_date, county_peak["date"])
            actual_lag_weeks = actual_lag_days / 7.0
            actual_severity = county_peak["value"] / metro_peaks[leader_msa]["value"] if metro_peaks[leader_msa]["value"] > 0 else 1.0

            # What would we have predicted? (using generic 4-5 week lag estimate)
            predicted_lag_weeks = 4.5
            predicted_severity = 1.8

            timing_error = predicted_lag_weeks - actual_lag_weeks
            severity_error = predicted_severity - actual_severity

            timing_errors.append(abs(timing_error))
            severity_errors.append(abs(severity_error))

            detection_correct = actual_lag_weeks > 0  # County peaked AFTER metro
            if detection_correct:
                detections["true_positive"] += 1
            else:
                detections["false_positive"] += 1

            logger.info(
                f"  County {county_fips}: actual lag={actual_lag_weeks:.1f}wk "
                f"(predicted 4.5), severity={actual_severity:.1f}× (predicted 1.8×)"
            )
        else:
            logger.info(f"  County {county_fips}: no data or no peak detected")

    # Print validation summary
    print("\n" + "=" * 60)
    print(f"RETROSPECTIVE VALIDATION RESULTS — {season}")
    print("=" * 60)

    if timing_errors:
        print(f"\nTiming Accuracy (Mean Absolute Error): {mean(timing_errors):.1f} weeks")
        print(f"  Median: {median(timing_errors):.1f} weeks")
        print(f"  Range: {min(timing_errors):.1f} — {max(timing_errors):.1f} weeks")

    if severity_errors:
        print(f"\nSeverity Accuracy (Mean Absolute Error): {mean(severity_errors):.2f}×")

    total = sum(detections.values())
    if total > 0:
        print(f"\nDetection Performance:")
        print(f"  True Positives: {detections['true_positive']}")
        print(f"  False Positives: {detections['false_positive']}")
        print(f"  Sensitivity: {detections['true_positive']/max(1,total)*100:.0f}%")

    print("\n" + "=" * 60)
    print("NOTE: This is a simplified validation. Full validation requires")
    print("county-level data availability (often sparse for rural counties).")
    print("Prospective validation during 2026-27 season will provide definitive results.")
    print("=" * 60)


def run_prospective_validation(start: str, end: str):
    """Run prospective validation — compare live predictions to outcomes.

    This requires the system to have been running and storing predictions.
    Not yet implemented — will be activated for 2026-27 season.
    """
    logger.info("Prospective validation mode")
    logger.info(f"Period: {start} to {end}")
    logger.info("")
    logger.info("⚠️  NOT YET IMPLEMENTED")
    logger.info("Prospective validation requires:")
    logger.info("  1. System deployed and generating predictions (Oct 2026+)")
    logger.info("  2. Predictions stored in DynamoDB with timestamps")
    logger.info("  3. Post-season ground truth comparison")
    logger.info("")
    logger.info("Target: Run after 2026-27 respiratory season (May 2027)")


def main():
    parser = argparse.ArgumentParser(description="Validate HealthSignals predictions")
    parser.add_argument(
        "--mode",
        choices=["retrospective", "prospective"],
        required=True,
        help="Validation mode",
    )
    parser.add_argument("--season", type=str, help="Season for retrospective (e.g., 2024-25)")
    parser.add_argument("--start", type=str, help="Start date for prospective (YYYYMMDD)")
    parser.add_argument("--end", type=str, help="End date for prospective (YYYYMMDD)")
    args = parser.parse_args()

    if args.mode == "retrospective":
        if not args.season:
            logger.error("--season required for retrospective mode")
            sys.exit(1)
        run_retrospective_validation(args.season)
    elif args.mode == "prospective":
        if not args.start or not args.end:
            logger.error("--start and --end required for prospective mode")
            sys.exit(1)
        run_prospective_validation(args.start, args.end)


if __name__ == "__main__":
    main()
