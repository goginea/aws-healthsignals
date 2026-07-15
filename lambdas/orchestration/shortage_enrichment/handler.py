"""Shortage Enrichment Lambda — Enriches disease outbreak alerts with drug shortage context.

Subscribes to EventBridge event: healthsignals.disease.threshold_crossed

When a disease outbreak is detected by the pipeline coordinator, this Lambda:
    1. Receives the disease detection event via EventBridge
    2. Queries the shortage-state DynamoDB table for medications relevant to the disease
    3. If relevant shortages exist, starts a Step Functions execution with alert_type="combined"
       that includes both disease context and shortage context
    4. If no relevant shortages, does nothing (disease-only alert already handled by coordinator)

This Lambda is part of the Drug Shortage Intelligence plugin module and has no
coupling to the core pipeline coordinator code.

Environment Variables:
    SHORTAGE_STATE_TABLE: DynamoDB table for shortage state (therapeutic-category-index GSI)
    STATE_MACHINE_ARN: Step Functions ARN for alert generation
    CONFIG_BUCKET: S3 bucket containing config files
    CONFIG_PREFIX: S3 key prefix for config files
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr

# Add shared module to path
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import _load_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# AWS clients
dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")
s3_client = boto3.client("s3")

# Configuration
SHORTAGE_STATE_TABLE = os.environ.get(
    "SHORTAGE_STATE_TABLE", "healthsignals-drug-shortage-state"
)
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")
CONFIG_PREFIX = os.environ.get("CONFIG_PREFIX", "config/")

shortage_state_table = dynamodb.Table(SHORTAGE_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Handle EventBridge event for disease threshold crossing.

    EventBridge detail format:
    {
        "disease_key": "influenza",
        "state_key": "texas",
        "week": "202645",
        "leader": {"msa_code": "26420", "metro_name": "Houston", "value": 12.5},
        "county_alerts": [
            {
                "county_fips": "48143",
                "county_name": "Erath County",
                "disease": "influenza",
                "leader_metro_name": "Houston",
                "leader_value": 12.5,
                "detection_week": "202645",
                "lag_weeks": 4,
                "severity_multiplier": 1.5,
                "confidence": 0.72,
                "seasons_calibrated": 3,
                "warning_window_weeks": 4,
                "cdc_activity_level": "high",
                ...
            }
        ]
    }
    """
    logger.info(f"Shortage enrichment triggered: {json.dumps(event, default=str)[:500]}")

    # Extract detail from EventBridge event
    detail = event.get("detail", {})
    disease_key = detail.get("disease_key")
    county_alerts = detail.get("county_alerts", [])

    if not disease_key:
        logger.warning("No disease_key in event detail, skipping")
        return {"statusCode": 400, "enriched": False, "reason": "missing disease_key"}

    if not county_alerts:
        logger.warning("No county_alerts in event detail, skipping")
        return {"statusCode": 400, "enriched": False, "reason": "no county_alerts"}

    # Query shortage context for the detected disease
    shortage_context = query_shortage_context(disease_key)

    if not shortage_context:
        logger.info(
            f"No relevant shortages for {disease_key} — "
            "disease-only alerts already handled by coordinator"
        )
        return {
            "statusCode": 200,
            "enriched": False,
            "reason": "no_relevant_shortages",
            "disease_key": disease_key,
        }

    # Shortages found — start combined alert generation for each county
    logger.info(
        f"Found {shortage_context['shortage_count']} shortage(s) for {disease_key}. "
        f"Starting combined alerts for {len(county_alerts)} counties."
    )

    executions_started = 0
    errors = []

    for county_alert in county_alerts:
        try:
            execution_arn = start_combined_alert(county_alert, shortage_context)
            if execution_arn:
                executions_started += 1
        except Exception as e:
            error_msg = f"Failed to start combined alert for {county_alert.get('county_name')}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

    result = {
        "statusCode": 200,
        "enriched": True,
        "disease_key": disease_key,
        "shortage_count": shortage_context["shortage_count"],
        "categories": shortage_context["categories"],
        "combined_executions_started": executions_started,
        "errors": errors[:5],
    }

    logger.info(f"Shortage enrichment complete: {json.dumps(result, default=str)}")
    return result


def query_shortage_context(disease_key: str) -> Optional[dict]:
    """Query shortage state for medications relevant to a disease outbreak.

    Looks up therapeutic categories associated with the given disease_key,
    then queries DynamoDB for current week shortage records in those categories
    with shortage_status NEW or WORSENING.

    Args:
        disease_key: The disease key from the outbreak detection (e.g., "influenza").

    Returns:
        None if no relevant shortages found, or a dict containing:
        {
            "affected_products": [...],
            "categories": ["Antivirals", ...],
            "disease_key": str,
            "shortage_count": int
        }
    """
    # Load therapeutic category config
    try:
        categories_config = _load_therapeutic_categories()
    except Exception as e:
        logger.error(f"Failed to load therapeutic categories config: {e}")
        return None

    # Find categories where disease_key is in relevant_diseases
    relevant_categories = []
    for category in categories_config.get("categories", []):
        if disease_key in category.get("relevant_diseases", []):
            relevant_categories.append(category)

    if not relevant_categories:
        logger.info(f"No therapeutic categories map to disease: {disease_key}")
        return None

    # Query DynamoDB for current week shortage records in relevant categories
    current_week = _get_current_iso_week()
    affected_products = []
    affected_category_names = []

    for category in relevant_categories:
        category_key = category["category_key"]
        display_name = category.get("display_name", category_key)

        try:
            response = shortage_state_table.query(
                IndexName="therapeutic-category-index",
                KeyConditionExpression=(
                    Key("therapeutic_category").eq(category_key)
                    & Key("week_timestamp").eq(current_week)
                ),
                FilterExpression=(
                    Attr("shortage_status").is_in(["NEW", "WORSENING"])
                ),
            )

            items = response.get("Items", [])
            if items:
                affected_category_names.append(display_name)
                for item in items:
                    affected_products.append({
                        "product_id": item.get("product_id"),
                        "product_name": item.get("product_name"),
                        "therapeutic_category": display_name,
                        "supply_status": item.get("supply_status"),
                        "shortage_status": item.get("shortage_status"),
                        "reason_for_shortage": item.get("reason_for_shortage"),
                        "estimated_resolution_date": item.get("estimated_resolution_date"),
                    })

        except Exception as e:
            logger.error(f"Error querying shortage state for category {category_key}: {e}")
            continue

    if not affected_products:
        return None

    return {
        "affected_products": affected_products,
        "categories": affected_category_names,
        "disease_key": disease_key,
        "shortage_count": len(affected_products),
    }


def start_combined_alert(county_alert: dict, shortage_context: dict) -> Optional[str]:
    """Start a Step Functions execution for a combined disease+shortage alert.

    Args:
        county_alert: County alert data from the disease detection event.
        shortage_context: Shortage context with affected_products and categories.

    Returns:
        Execution ARN if successful, None otherwise.
    """
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not set — skipping combined alert")
        return None

    county_fips = county_alert.get("county_fips", "unknown")
    disease = county_alert.get("disease", "unknown")
    week = county_alert.get("detection_week", "unknown")

    # Build unique execution name
    exec_name = f"combined-{county_fips}-{disease}-{week}-{_short_id()}"
    exec_name = exec_name[:80].replace(" ", "_")

    # Build Step Functions input for combined alert
    confidence = county_alert.get("confidence", 0.6)

    sfn_input = {
        "county_fips": county_fips,
        "county_name": county_alert.get("county_name", "Unknown County"),
        "disease": disease,
        "leader_metro_name": county_alert.get("leader_metro_name", "Unknown"),
        "leader_value": county_alert.get("leader_value", 0),
        "detection_week": week,
        "lag_weeks": county_alert.get("lag_weeks", 4),
        "severity_multiplier": county_alert.get("severity_multiplier", 1.5),
        "confidence": confidence,
        "confidence_pct": int(confidence * 100),
        "seasons_calibrated": county_alert.get("seasons_calibrated", 3),
        "warning_window_weeks": county_alert.get("warning_window_weeks", 4),
        "cdc_activity_level": county_alert.get("cdc_activity_level", "unknown"),
        "alert_contacts": county_alert.get("alert_contacts", []),
        "alert_type": "combined",
        "shortage_context": shortage_context,
    }

    response = sfn_client.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=exec_name,
        input=json.dumps(sfn_input, default=str),
    )

    logger.info(f"Started combined SFN execution: {exec_name}")
    return response["executionArn"]


# === Helpers ===


def _load_therapeutic_categories() -> dict:
    """Load therapeutic category configuration."""
    return _load_config("shortage_monitoring/therapeutic_categories.json")


def _get_current_iso_week() -> str:
    """Get current ISO week as YYYY-WNN format."""
    now = datetime.now(timezone.utc)
    iso_cal = now.isocalendar()
    return f"{iso_cal[0]}-W{iso_cal[1]:02d}"


def _short_id() -> str:
    """Generate a short unique identifier for execution names."""
    import uuid
    return uuid.uuid4().hex[:8]
