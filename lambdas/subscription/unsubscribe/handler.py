"""Unsubscribe Handler — Processes unsubscribe requests.

GET /subscription/unsubscribe?token=[REDACTED_PARAM] (from email link — one-click)
POST /subscription/unsubscribe (programmatic with subscription_id)

Soft-deletes: marks subscription as "inactive" but retains the record.
"""
import json
import os
import sys
import logging
from datetime import datetime
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
from config_loader import get_system_config
from token_utils import validate_token, TokenError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
SUBSCRIPTIONS_TABLE = os.environ.get(
    "SUBSCRIPTIONS_TABLE", system.get("dynamodb_tables", {}).get("subscriptions", "healthsignals-subscriptions")
)
SENDER_EMAIL = system.get("delivery", {}).get("ses_sender_email", "alerts@healthsignals.example.com")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(SUBSCRIPTIONS_TABLE)
ses = boto3.client("ses")


def lambda_handler(event: dict, context: Any) -> dict:
    """Process unsubscribe request.

    Two modes:
    1. GET with ?token=... (one-click from email — token contains county_fips + sub_id)
    2. POST with body: {"subscription_id": "...", "county_fips": "..."}
    """
    http_method = event.get("httpMethod", "GET")

    try:
        if http_method == "GET":
            return _handle_token_unsubscribe(event)
        else:
            return _handle_post_unsubscribe(event)
    except TokenError as e:
        return _response(401, {"error": "invalid_token", "message": str(e)})
    except Exception as e:
        logger.error(f"Unsubscribe failed: {e}", exc_info=True)
        return _response(500, {"error": "internal_error", "message": "Unsubscribe failed."})


def _handle_token_unsubscribe(event: dict) -> dict:
    """Handle one-click unsubscribe via signed token."""
    params = event.get("queryStringParameters") or {}
    token = params.get("token", "")

    if not token:
        return _response(400, {"error": "Missing 'token' parameter"})

    payload = validate_token(token, expected_purpose="unsubscribe")
    county_fips = payload["fips"]
    subscription_id = payload["sub"]

    return _deactivate_subscription(county_fips, subscription_id, reason="email_link")


def _handle_post_unsubscribe(event: dict) -> dict:
    """Handle programmatic unsubscribe via POST body."""
    body = event.get("body", "")
    if isinstance(body, str):
        body = json.loads(body) if body else {}

    county_fips = body.get("county_fips")
    subscription_id = body.get("subscription_id")

    if not county_fips or not subscription_id:
        return _response(400, {"error": "Missing county_fips and/or subscription_id"})

    return _deactivate_subscription(county_fips, subscription_id, reason="api_request")


def _deactivate_subscription(county_fips: str, subscription_id: str, reason: str) -> dict:
    """Mark subscription as inactive (soft delete)."""
    # Fetch current record
    response = table.get_item(
        Key={"county_fips": county_fips, "subscription_id": subscription_id}
    )
    item = response.get("Item")

    if not item:
        return _response(404, {"error": "Subscription not found"})

    if item.get("status") == "inactive":
        return _response(200, {"message": "Already unsubscribed.", "county_name": item.get("county_name")})

    # Soft delete
    now = datetime.utcnow().isoformat()
    table.update_item(
        Key={"county_fips": county_fips, "subscription_id": subscription_id},
        UpdateExpression="SET #s = :status, updated_at = :now, unsubscribed_at = :now, unsubscribe_reason = :reason",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "inactive",
            ":now": now,
            ":reason": reason,
        },
    )

    # Send confirmation
    _send_unsubscribe_confirmation(item)

    logger.info(f"Unsubscribed: {subscription_id} for {item.get('county_name')} (reason: {reason})")

    return _response(200, {
        "message": "Successfully unsubscribed.",
        "county_name": item.get("county_name"),
        "county_fips": county_fips,
        "note": "You will no longer receive HealthSignals alerts. You can re-subscribe at any time.",
    })


def _send_unsubscribe_confirmation(subscription: dict) -> None:
    """Send confirmation that unsubscribe was processed."""
    email = subscription.get("contact_email")
    name = subscription.get("contact_name", "Health Officer")
    county = subscription.get("county_name", "your county")

    subject = f"HealthSignals — {county} unsubscribed"
    body = f"""Hello {name},

This confirms that {county} has been unsubscribed from HealthSignals alerts.

You will no longer receive disease preparedness briefs or SMS notifications.

If this was a mistake, you can re-subscribe at any time by contacting your state
health department or visiting the HealthSignals subscription portal.

Thank you for using HealthSignals.

—
Amazon HealthSignals Team
"""
    try:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
    except Exception as e:
        logger.error(f"Unsubscribe confirmation email failed for {email}: {e}")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
