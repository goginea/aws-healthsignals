"""Leader Detection — Config-driven threshold crossing detection.

Reads detection thresholds from disease configs and metro info from state configs.
No hardcoded values — all thresholds come from config/diseases/*.json.
"""
import json
import os
import sys
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_disease_config,
    get_detection_threshold,
    get_all_sentinel_metros,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
ALERT_STATE_TABLE = os.environ.get(
    "ALERT_STATE_TABLE", system["dynamodb_tables"]["alert_state"]
)

dynamodb = boto3.resource("dynamodb")
alert_state_table = dynamodb.Table(ALERT_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Detect if any sentinel metro has crossed threshold for any tracked disease.

    Input event:
    {
        "disease": "influenza",
        "state_key": "texas",  (optional — if omitted, uses all states)
        "week": "202645",
        "metro_signals": {
            "26420": {"value": 3.2, "trend": "rising"},
            "19100": {"value": 1.8, "trend": "rising"},
            ...
        }
    }
    """
    disease_key = event.get("disease", "influenza")
    state_key = event.get("state_key")
    week = event.get("week")
    metro_signals = event.get("metro_signals", {})

    if not metro_signals:
        return {"detected": False, "reason": "No metro signals provided"}

    # Load threshold from disease config (with optional state override)
    threshold_config = get_detection_threshold(disease_key, state_key)
    threshold_value = threshold_config["threshold_pct_ed_visits"]
    require_rising = threshold_config.get("require_rising_trend", True)

    # Load metro names for enrichment
    all_metros = get_all_sentinel_metros()

    # Check each metro against threshold
    leaders = []
    for msa_code, signal_data in metro_signals.items():
        value = signal_data.get("value", 0)
        trend = signal_data.get("trend", "unknown")

        if is_threshold_crossed(value, trend, threshold_value, require_rising):
            metro_info = all_metros.get(msa_code, {})
            leaders.append({
                "msa_code": msa_code,
                "metro_name": metro_info.get("short_name", msa_code),
                "state": metro_info.get("state_abbreviation", "??"),
                "value": value,
                "trend": trend,
                "crossed_at_week": week,
            })

    if not leaders:
        return {
            "detected": False,
            "disease": disease_key,
            "week": week,
            "threshold_used": threshold_value,
            "reason": "No metro has crossed threshold",
            "highest_signal": max(
                metro_signals.items(), key=lambda x: x[1].get("value", 0)
            ) if metro_signals else None,
        }

    # Sort by value — highest signal = strongest leader
    leaders.sort(key=lambda x: x["value"], reverse=True)
    primary_leader = leaders[0]

    # Check if this is a NEW detection
    season = determine_season(week)
    already_alerted = check_existing_alert(disease_key, season, primary_leader["msa_code"])

    if already_alerted:
        return {
            "detected": True,
            "new_alert": False,
            "disease": disease_key,
            "leader": primary_leader,
            "reason": "Leader already detected this season — monitoring continues",
        }

    # Record new leader detection
    record_leader_detection(disease_key, season, primary_leader, week)

    return {
        "detected": True,
        "new_alert": True,
        "disease": disease_key,
        "week": week,
        "season": season,
        "leader": primary_leader,
        "all_leaders": leaders,
        "threshold_used": threshold_value,
        "trigger_prediction_pipeline": True,
    }


def is_threshold_crossed(
    value: float, trend: str, threshold: float, require_rising: bool
) -> bool:
    """Determine if a metro signal has crossed the detection threshold."""
    if value < threshold:
        return False
    if require_rising and trend != "rising":
        return False
    return True


def determine_season(week: str) -> str:
    """Map epiweek to respiratory season (e.g., '2024-25')."""
    year = int(week[:4])
    week_num = int(week[4:])
    if week_num >= 40:
        return f"{year}-{str(year + 1)[-2:]}"
    else:
        return f"{year - 1}-{str(year)[-2:]}"


def check_existing_alert(disease: str, season: str, msa_code: str) -> bool:
    """Check if we already detected a leader for this disease/season."""
    try:
        response = alert_state_table.get_item(
            Key={
                "county_fips": f"LEADER_{msa_code}",
                "disease_season": f"{disease}_{season}",
            }
        )
        return "Item" in response
    except Exception as e:
        logger.error(f"DynamoDB check failed: {e}")
        return False


def record_leader_detection(disease: str, season: str, leader: dict, week: str) -> None:
    """Record the leader detection in DynamoDB."""
    try:
        alert_state_table.put_item(
            Item={
                "county_fips": f"LEADER_{leader['msa_code']}",
                "disease_season": f"{disease}_{season}",
                "detected_week": week,
                "metro_name": leader.get("metro_name", ""),
                "value_at_detection": Decimal(str(leader["value"])),
                "detected_at": datetime.utcnow().isoformat(),
            }
        )
    except Exception as e:
        logger.error(f"Failed to record leader detection: {e}")
        raise
