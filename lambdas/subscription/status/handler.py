"""Status Handler — Check subscription status and history.

GET /subscription/status?county_fips=48143&subscription_id=uuid-...
Returns subscription details, last alert, and health metrics.
"""
import json
import os
import sys
import logging
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
from config_loader import get_system_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
SUBSCRIPTIONS_TABLE = os.environ.get(
    "SUBSCRIPTIONS_TABLE", system.get("dynamodb_tables", {}).get("subscriptions", "healthsignals-subscriptions")
)
ALERT_STATE_TABLE = os.environ.get(
    "ALERT_STATE_TABLE", system.get("dynamodb_tables", {}).get("alert_state", "healthsignals-alert-state")
)

dynamodb = boto3.resource("dynamodb")
sub_table = dynamodb.Table(SUBSCRIPTIONS_TABLE)
alert_table = dynamodb.Table(ALERT_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Return subscription status and recent alert history.

    Query params:
    - county_fips (required)
    - subscription_id (optional — if omitted, returns all subscriptions for county)
    """
    try:
        params = event.get("queryStringParameters") or {}
        county_fips = params.get("county_fips")

        if not county_fips:
            return _response(400, {"error": "Missing required parameter: county_fips"})

        subscription_id = params.get("subscription_id")

        if subscription_id:
            # Fetch single subscription
            return _get_single_subscription(county_fips, subscription_id)
        else:
            # Fetch all subscriptions for this county
            return _get_county_subscriptions(county_fips)

    except Exception as e:
        logger.error(f"Status check failed: {e}", exc_info=True)
        return _response(500, {"error": "internal_error", "message": "Status check failed."})


def _get_single_subscription(county_fips: str, subscription_id: str) -> dict:
    """Get details for a single subscription."""
    response = sub_table.get_item(
        Key={"county_fips": county_fips, "subscription_id": subscription_id}
    )
    item = response.get("Item")

    if not item:
        return _response(404, {"error": "Subscription not found"})

    # Get recent alert history for this county
    alerts = _get_recent_alerts(county_fips)

    # Sanitize: don't expose internal tokens
    safe_item = _sanitize_subscription(item)
    safe_item["recent_alerts"] = alerts
    safe_item["health"] = _assess_health(item, alerts)

    return _response(200, safe_item)


def _get_county_subscriptions(county_fips: str) -> dict:
    """Get all subscriptions for a county."""
    response = sub_table.query(
        KeyConditionExpression=Key("county_fips").eq(county_fips)
    )
    items = response.get("Items", [])

    if not items:
        return _response(404, {"error": f"No subscriptions found for county {county_fips}"})

    # Sanitize all items
    safe_items = [_sanitize_subscription(item) for item in items]

    return _response(200, {
        "county_fips": county_fips,
        "subscription_count": len(safe_items),
        "subscriptions": safe_items,
    })


def _get_recent_alerts(county_fips: str, limit: int = 5) -> list:
    """Get recent alerts sent to this county from the alert state table."""
    try:
        response = alert_table.query(
            KeyConditionExpression=Key("county_fips").eq(county_fips),
            ScanIndexForward=False,  # Most recent first
            Limit=limit,
        )
        items = response.get("Items", [])
        return [
            {
                "disease": item.get("disease_season", "").split("_")[0],
                "season": item.get("disease_season", "").split("_")[-1] if "_" in item.get("disease_season", "") else "",
                "detected_week": item.get("detected_week"),
                "severity": item.get("severity"),
                "sent_at": item.get("detected_at"),
            }
            for item in items
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch alerts for {county_fips}: {e}")
        return []


def _assess_health(subscription: dict, alerts: list) -> dict:
    """Assess subscription health — is everything working?"""
    status = subscription.get("status", "unknown")

    issues = []
    if status == "pending_verification":
        issues.append("Email not yet verified — no alerts will be sent")
    if status == "paused":
        issues.append(f"Alerts paused until {subscription.get('pause_until', 'unknown')}")
    if not subscription.get("contact_email"):
        issues.append("No email configured")
    if "sms" in subscription.get("delivery_preferences", {}).get("channels", []):
        if not subscription.get("contact_phone"):
            issues.append("SMS configured but no phone number")

    return {
        "status": "healthy" if not issues and status == "active" else "attention_needed",
        "issues": issues,
        "alerts_received_recently": len(alerts),
    }


def _sanitize_subscription(item: dict) -> dict:
    """Remove sensitive internal fields before returning to client."""
    sensitive_fields = {"verification_token", "unsubscribe_token", "metadata"}
    sanitized = {k: v for k, v in item.items() if k not in sensitive_fields}
    # Ensure therapeutic_categories is always present (empty array for legacy subscriptions)
    if "therapeutic_categories" not in sanitized:
        sanitized["therapeutic_categories"] = []
    return sanitized


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body, default=str),
    }
