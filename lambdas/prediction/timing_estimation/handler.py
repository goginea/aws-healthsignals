"""Timing Estimation — Config-driven historical lag lookup + severity projection.

Reads prediction parameters from disease configs and calibration data from DynamoDB.
No hardcoded values — defaults come from config/diseases/*.json.

The prediction is: historical_median_lag ± confidence_interval
This is NOT AI/ML — it's a lookup table with statistics.
"""
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from statistics import median, stdev

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_disease_config,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
CALIBRATION_TABLE = os.environ.get(
    "CALIBRATION_TABLE", system["dynamodb_tables"]["calibration"]
)

dynamodb = boto3.resource("dynamodb")
calibration_table = dynamodb.Table(CALIBRATION_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Estimate timing and severity for each affected county.

    Input event (from geographic_affinity):
    {
        "leader_msa": "26420",
        "leader_name": "Houston",
        "disease": "influenza",
        "week": "202645",
        "affected_counties": [
            {"county_fips": "48143", "county_name": "Erath County", "affinity_weight": 0.75, ...}
        ]
    }

    Returns:
        Enriched county list with timing estimates and severity projections.
    """
    leader_msa = event.get("leader_msa")
    disease_key = event.get("disease")
    week = event.get("week")
    affected_counties = event.get("affected_counties", [])

    # Support single-county invocation (from pipeline_coordinator per-county calls)
    if not affected_counties and event.get("county_fips"):
        affected_counties = [{
            "county_fips": event["county_fips"],
            "county_name": event.get("county_name", "Unknown"),
            "affinity_weight": event.get("affinity_weight", 1.0),
        }]

    if not affected_counties:
        return {"estimates": [], "reason": "No affected counties"}

    # Load disease prediction defaults from config
    disease_config = get_disease_config(disease_key)
    prediction_defaults = disease_config.get("prediction", {})
    default_lag_range = prediction_defaults.get("typical_lag_range_weeks", [3, 6])
    default_multiplier_range = prediction_defaults.get("severity_multiplier_typical_range", [1.0, 3.0])
    confidence_degrades_after = prediction_defaults.get("confidence_degrades_after_seasons", 2)

    estimates = []
    for county in affected_counties:
        county_fips = county["county_fips"]
        county_name = county["county_name"]

        # Look up historical calibration data from DynamoDB
        calibration = get_calibration_data(county_fips, leader_msa, disease_key)

        if calibration and len(calibration) >= 2:
            # Use historical data
            lag_weeks = calculate_lag_estimate(calibration)
            severity_mult = calculate_severity_multiplier(calibration)
            confidence = calculate_confidence(calibration, confidence_degrades_after)
            seasons_calibrated = len(calibration)
        else:
            # Fall back to disease config defaults
            lag_weeks = {
                "median": sum(default_lag_range) / 2,
                "min": default_lag_range[0],
                "max": default_lag_range[1],
            }
            severity_mult = {
                "median": sum(default_multiplier_range) / 2,
                "min": default_multiplier_range[0],
                "max": default_multiplier_range[1],
            }
            confidence = 0.3  # Low confidence without historical data
            seasons_calibrated = 0

        # Calculate expected arrival date
        detection_week_num = int(week[4:])
        expected_arrival_weeks = lag_weeks["median"]

        estimates.append({
            **county,
            "timing": {
                "lag_weeks_median": lag_weeks["median"],
                "lag_weeks_min": lag_weeks["min"],
                "lag_weeks_max": lag_weeks["max"],
                "expected_arrival_weeks_from_now": expected_arrival_weeks,
            },
            "severity": {
                "multiplier_median": severity_mult["median"],
                "multiplier_min": severity_mult["min"],
                "multiplier_max": severity_mult["max"],
            },
            "confidence": confidence,
            "seasons_calibrated": seasons_calibrated,
            "warning_window_weeks": expected_arrival_weeks,
            "data_source": "historical_calibration" if seasons_calibrated >= 2 else "disease_defaults",
        })

    # Sort by urgency (shortest lag first)
    estimates.sort(key=lambda x: x["timing"]["lag_weeks_median"])

    # --- External Forecast Enrichment (optional, from Forecast Provider plugin) ---
    external_forecast = None
    forecast_state_table_name = os.environ.get("FORECAST_STATE_TABLE", "")
    state_key = event.get("state_key", "")

    if forecast_state_table_name and state_key and disease_key:
        external_forecast = _query_external_forecast(
            forecast_state_table_name, state_key, disease_key, week
        )

    result = {
        "disease": disease_key,
        "leader_msa": leader_msa,
        "week": week,
        "estimates": estimates,
        "total_counties": len(estimates),
        "external_forecast": external_forecast,
    }

    # For single-county calls (from pipeline_coordinator), also return flat fields
    # that the coordinator expects for building the SFN input
    if len(estimates) == 1:
        est = estimates[0]
        result["estimated_lag_weeks"] = est["timing"]["lag_weeks_median"]
        result["severity_multiplier"] = est["severity"]["multiplier_median"]
        result["confidence"] = est["confidence"]
        result["seasons_calibrated"] = est["seasons_calibrated"]
        result["warning_window_weeks"] = est["timing"]["expected_arrival_weeks_from_now"]
        result["cdc_activity_level"] = est.get("cdc_activity_level", "unknown")

    return result


def get_calibration_data(county_fips: str, leader_msa: str, disease: str) -> list:
    """Retrieve historical lag data from DynamoDB calibration table."""
    try:
        response = calibration_table.query(
            KeyConditionExpression="county_fips = :fips AND begins_with(disease_season, :prefix)",
            ExpressionAttributeValues={
                ":fips": county_fips,
                ":prefix": f"{disease}_{leader_msa}_",
            },
        )
        return response.get("Items", [])
    except Exception as e:
        logger.error(f"Calibration lookup failed for {county_fips}: {e}")
        return []


def calculate_lag_estimate(calibration: list) -> dict:
    """Calculate lag statistics from historical calibration data."""
    lags = [float(item.get("lag_weeks", 0)) for item in calibration if item.get("lag_weeks")]
    if not lags:
        return {"median": 4.0, "min": 3.0, "max": 6.0}

    med = median(lags)
    if len(lags) >= 3:
        sd = stdev(lags)
        return {"median": med, "min": max(1, med - sd), "max": med + sd}
    else:
        return {"median": med, "min": min(lags), "max": max(lags)}


def calculate_severity_multiplier(calibration: list) -> dict:
    """Calculate severity multiplier from historical data."""
    multipliers = [
        float(item.get("severity_multiplier", 1.0))
        for item in calibration
        if item.get("severity_multiplier")
    ]
    if not multipliers:
        return {"median": 2.0, "min": 1.0, "max": 3.0}

    med = median(multipliers)
    if len(multipliers) >= 3:
        sd = stdev(multipliers)
        return {"median": med, "min": max(0.5, med - sd), "max": med + sd}
    else:
        return {"median": med, "min": min(multipliers), "max": max(multipliers)}


def calculate_confidence(calibration: list, degrades_after: int) -> float:
    """Calculate confidence score based on data quality."""
    n_seasons = len(calibration)
    if n_seasons >= 5:
        base_confidence = 0.85
    elif n_seasons >= 3:
        base_confidence = 0.70
    elif n_seasons >= 2:
        base_confidence = 0.55
    else:
        base_confidence = 0.30

    # Degrade if most recent calibration is old
    # (future: check dates of calibration entries)
    return round(base_confidence, 2)


def _query_external_forecast(
    table_name: str, state_key: str, disease_key: str, week: str
) -> dict | None:
    """Query external forecast from the forecast-state DynamoDB table.

    Looks for the aggregated forecast record (provider='_aggregated') for the
    given state + disease + week. Returns the forecast dict or None.

    This function is only called when FORECAST_STATE_TABLE env var is set
    (i.e., when the Forecast Provider plugin is deployed).
    """
    try:
        # Convert epiweek (YYYYWW) to ISO week (YYYY-WNN) if needed
        if len(week) == 6 and "W" not in week:
            iso_week = f"{week[:4]}-W{week[4:]}"
        else:
            iso_week = week

        forecast_table = dynamodb.Table(table_name)
        disease_week_key = f"{disease_key}_{iso_week}"

        response = forecast_table.get_item(
            Key={
                "geo_key": state_key,
                "disease_week": disease_week_key,
            }
        )

        item = response.get("Item")
        if not item:
            logger.info(f"No external forecast found for {state_key}/{disease_week_key}")
            return None

        # Return relevant forecast data for Bedrock prompt enrichment
        predictions = item.get("predictions", [])
        return {
            "provider": item.get("provider", "external"),
            "providers": item.get("providers", []),
            "provider_count": item.get("provider_count", 1),
            "predictions": predictions,
            "target": item.get("target", "hospitalizations"),
            "conflict": item.get("conflict", False),
            "agreement_status": item.get("agreement_status", "unknown"),
            "forecast_date": item.get("forecast_date", ""),
        }

    except Exception as e:
        logger.warning(f"External forecast query failed for {state_key}/{disease_key}: {e}")
        return None
