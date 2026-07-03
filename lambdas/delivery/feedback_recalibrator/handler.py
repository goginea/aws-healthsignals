"""Feedback Recalibrator — Updates calibration tables based on health officer feedback.

Triggered when enough feedback accumulates (≥3 responses for a county+disease+season).
Reads feedback records, calculates lag/severity adjustments, and updates the calibration
table with improved estimates.

Blending formula:
    new_calibration = (historical_weight × historical) + (feedback_weight × feedback_derived)
    Default: 70% historical, 30% feedback

This allows the system to self-correct over time while not overreacting to individual
feedback responses.
"""
import json
import os
import sys
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from statistics import median

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import get_system_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
FEEDBACK_TABLE = os.environ.get("FEEDBACK_TABLE", "healthsignals-feedback")
CALIBRATION_TABLE = os.environ.get(
    "CALIBRATION_TABLE", system["dynamodb_tables"]["calibration"]
)

# Blending weights
HISTORICAL_WEIGHT = float(os.environ.get("HISTORICAL_WEIGHT", "0.7"))
FEEDBACK_WEIGHT = float(os.environ.get("FEEDBACK_WEIGHT", "0.3"))
MIN_FEEDBACK_COUNT = int(os.environ.get("MIN_FEEDBACK_COUNT", "3"))

dynamodb = boto3.resource("dynamodb")
feedback_table = dynamodb.Table(FEEDBACK_TABLE)
calibration_table = dynamodb.Table(CALIBRATION_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Process accumulated feedback and update calibration tables.

    Can be triggered:
    1. By EventBridge schedule (e.g., monthly)
    2. By the feedback_collector when threshold is reached
    3. Manually for testing

    Input event (optional filters):
    {
        "county_fips": "48143",  # Optional: process only this county
        "disease": "influenza",  # Optional: process only this disease
        "season": "2026-27"      # Optional: process only this season
    }

    Returns:
        Summary of recalibration actions taken.
    """
    county_filter = event.get("county_fips")
    disease_filter = event.get("disease")
    season_filter = event.get("season")

    results = {"updated": [], "skipped": [], "errors": []}

    try:
        # Scan feedback table for actionable feedback groups
        feedback_groups = get_feedback_groups(county_filter, disease_filter, season_filter)

        for group_key, feedback_records in feedback_groups.items():
            if len(feedback_records) < MIN_FEEDBACK_COUNT:
                results["skipped"].append({
                    "group": group_key,
                    "reason": f"Only {len(feedback_records)} responses (need {MIN_FEEDBACK_COUNT})",
                })
                continue

            try:
                adjustment = calculate_adjustment(feedback_records)
                apply_calibration_update(group_key, adjustment)
                results["updated"].append({
                    "group": group_key,
                    "feedback_count": len(feedback_records),
                    "adjustment": adjustment,
                })
                logger.info(f"Recalibrated {group_key}: {adjustment}")
            except Exception as e:
                results["errors"].append({"group": group_key, "error": str(e)})
                logger.error(f"Recalibration failed for {group_key}: {e}")

    except Exception as e:
        results["errors"].append({"error": f"Top-level failure: {str(e)}"})
        logger.error(f"Recalibration job failed: {e}")

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results, default=str),
    }


def get_feedback_groups(
    county_filter: str = None,
    disease_filter: str = None,
    season_filter: str = None,
) -> dict:
    """Retrieve and group feedback records by county+disease+season.

    Returns:
        Dict mapping "county_fips:disease:season" → [feedback_records]
    """
    # Scan feedback table (in production, use a GSI or scheduled batch)
    scan_kwargs = {}
    filter_expressions = []

    if county_filter:
        filter_expressions.append("county_fips = :fips")
        scan_kwargs.setdefault("ExpressionAttributeValues", {})[":fips"] = county_filter

    response = feedback_table.scan(**scan_kwargs) if not scan_kwargs else feedback_table.scan(
        FilterExpression=" AND ".join(filter_expressions) if filter_expressions else None,
        **{k: v for k, v in scan_kwargs.items() if k != "FilterExpression"},
    )

    records = response.get("Items", [])

    # Group by county + disease extracted from alert_id
    groups = {}
    for record in records:
        alert_id = record.get("alert_id", "")
        county_fips = record.get("county_fips", "")

        # alert_id format: "{disease}_{county_fips}_{week}"
        parts = alert_id.split("_")
        if len(parts) >= 2:
            disease = parts[0]
            # Determine season from week if present
            week = parts[-1] if len(parts) >= 3 else "unknown"
            season = determine_season_from_week(week)
        else:
            continue

        # Apply filters
        if disease_filter and disease != disease_filter:
            continue
        if season_filter and season != season_filter:
            continue

        group_key = f"{county_fips}:{disease}:{season}"
        groups.setdefault(group_key, []).append(record)

    return groups


def calculate_adjustment(feedback_records: list) -> dict:
    """Calculate calibration adjustments from feedback.

    Timing feedback: "early" → increase lag, "late" → decrease lag, "on-time" → no change
    Severity feedback: "over" → decrease multiplier, "under" → increase, "about-right" → no change

    Returns:
        Dict with lag_adjustment and severity_adjustment values.
    """
    lag_adjustments = []
    severity_adjustments = []

    for record in feedback_records:
        timing = record.get("timing_accuracy", "")
        severity = record.get("severity_accuracy", "")
        occurred = record.get("outbreak_occurred")

        # Timing adjustment (-1 = arrived sooner than predicted, +1 = arrived later)
        if timing == "early":
            lag_adjustments.append(-1.0)  # We predicted too much lag
        elif timing == "late":
            lag_adjustments.append(1.0)   # We predicted too little lag
        elif timing == "on-time":
            lag_adjustments.append(0.0)

        # Severity adjustment
        if severity == "over":
            severity_adjustments.append(-0.3)  # Reduce multiplier
        elif severity == "under":
            severity_adjustments.append(0.3)   # Increase multiplier
        elif severity == "about-right":
            severity_adjustments.append(0.0)

        # If outbreak didn't occur at all, big negative severity signal
        if occurred is False:
            severity_adjustments.append(-0.5)
            lag_adjustments.append(0.0)  # Can't assess timing if it didn't happen

    # Calculate median adjustments (robust to outliers)
    lag_adj = median(lag_adjustments) if lag_adjustments else 0.0
    sev_adj = median(severity_adjustments) if severity_adjustments else 0.0

    return {
        "lag_adjustment_weeks": round(lag_adj, 1),
        "severity_adjustment": round(sev_adj, 2),
        "feedback_count": len(feedback_records),
        "accuracy_rate": sum(
            1 for r in feedback_records if r.get("outbreak_occurred") is True
        ) / max(len(feedback_records), 1),
    }


def apply_calibration_update(group_key: str, adjustment: dict) -> None:
    """Apply blended calibration update to DynamoDB.

    Blending: new = (HISTORICAL_WEIGHT × current) + (FEEDBACK_WEIGHT × feedback_derived)
    """
    county_fips, disease, season = group_key.split(":")

    # Get current calibration
    try:
        response = calibration_table.get_item(
            Key={
                "county_fips": county_fips,
                "disease_season": f"{disease}_{season}",
            }
        )
        current = response.get("Item", {})
    except Exception:
        current = {}

    current_lag = float(current.get("lag_weeks", 4.0))
    current_severity = float(current.get("severity_multiplier", 2.0))

    # Apply blended adjustment
    feedback_lag = current_lag + adjustment["lag_adjustment_weeks"]
    feedback_severity = current_severity + adjustment["severity_adjustment"]

    new_lag = (HISTORICAL_WEIGHT * current_lag) + (FEEDBACK_WEIGHT * feedback_lag)
    new_severity = (HISTORICAL_WEIGHT * current_severity) + (FEEDBACK_WEIGHT * feedback_severity)

    # Clamp to reasonable ranges
    new_lag = max(1.0, min(12.0, new_lag))
    new_severity = max(0.5, min(10.0, new_severity))

    # Write updated calibration
    calibration_table.put_item(
        Item={
            "county_fips": county_fips,
            "disease_season": f"{disease}_{season}",
            "lag_weeks": Decimal(str(round(new_lag, 1))),
            "severity_multiplier": Decimal(str(round(new_severity, 2))),
            "last_recalibrated": datetime.utcnow().isoformat(),
            "feedback_count": adjustment["feedback_count"],
            "accuracy_rate": Decimal(str(round(adjustment["accuracy_rate"], 2))),
            "recalibration_method": "feedback_blended",
            "blend_weights": f"historical={HISTORICAL_WEIGHT},feedback={FEEDBACK_WEIGHT}",
        }
    )


def determine_season_from_week(week_str: str) -> str:
    """Convert epiweek string to season identifier."""
    try:
        year = int(week_str[:4])
        week_num = int(week_str[4:])
        if week_num >= 40:
            return f"{year}-{str(year + 1)[-2:]}"
        else:
            return f"{year - 1}-{str(year)[-2:]}"
    except (ValueError, IndexError):
        return "unknown"
