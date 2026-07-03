"""Verify Handler — Confirms email verification for double opt-in.

GET /subscription/verify?token=<signed_token>
Marks subscription as "active" — only verified subscriptions receive alerts.
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
    """Verify a subscription via signed token from confirmation email.

    Query params: ?token=<base64_payload>.<hmac_signature>
    """
    try:
        # Extract token from query params
        params = event.get("queryStringParameters") or {}
        token = params.get("token", "")

        if not token:
            return _response(400, {"error": "Missing 'token' parameter"})

        # Validate token
        payload = validate_token(token, expected_purpose="verification")
        county_fips = payload["fips"]
        subscription_id = payload["sub"]

        # Look up subscription
        response = table.get_item(
            Key={"county_fips": county_fips, "subscription_id": subscription_id}
        )
        item = response.get("Item")

        if not item:
            return _response(404, {"error": "Subscription not found"})

        if item.get("status") == "active":
            return _response(200, {
                "message": "Subscription already verified.",
                "county_name": item.get("county_name"),
                "status": "active",
            })

        if item.get("status") == "inactive":
            return _response(410, {"error": "Subscription has been cancelled."})

        # Update status to active
        now = datetime.utcnow().isoformat()
        table.update_item(
            Key={"county_fips": county_fips, "subscription_id": subscription_id},
            UpdateExpression="SET #s = :status, verified_at = :now, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "active",
                ":now": now,
            },
        )

        # Send welcome email
        _send_welcome_email(item)

        logger.info(f"Subscription verified: {subscription_id} for {item.get('county_name')}")

        return _response(200, {
            "message": "Subscription verified successfully!",
            "county_name": item.get("county_name"),
            "diseases": item.get("diseases", []),
            "status": "active",
            "note": "You will now receive HealthSignals alerts when disease activity is detected.",
        })

    except TokenError as e:
        logger.warning(f"Token validation failed: {e}")
        return _response(401, {
            "error": "invalid_token",
            "message": str(e),
            "hint": "Token may have expired (72h). Request a new verification email.",
        })
    except Exception as e:
        logger.error(f"Verification failed: {e}", exc_info=True)
        return _response(500, {"error": "internal_error", "message": "Verification failed."})


def _send_welcome_email(subscription: dict) -> None:
    """Send welcome email after successful verification."""
    email = subscription.get("contact_email")
    name = subscription.get("contact_name", "Health Officer")
    county = subscription.get("county_name", "your county")
    diseases = subscription.get("diseases", [])

    subject = f"Welcome to HealthSignals — {county} is now subscribed"
    body = f"""Hello {name},

Your HealthSignals subscription for {county} is now active.

What to expect:
- Monitoring: {', '.join(d.title() for d in diseases)}
- Delivery: {', '.join(subscription.get('delivery_preferences', {}).get('channels', ['email']))}
- Alerts when: Sentinel metro activity crosses threshold for your subscribed diseases

How it works:
1. We monitor disease signals in major metro areas (Houston, DFW, Austin, San Antonio)
2. When a metro crosses threshold, we calculate when your county will be affected
3. You receive a preparation brief with timing estimate and action checklist

Important:
- All alerts are advisory only — based on historical pattern analysis
- Confidence levels are always included
- You can pause or unsubscribe anytime via the link in any alert email

Stay prepared,
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
        logger.error(f"Welcome email failed for {email}: {e}")


def _response(status_code: int, body: dict) -> dict:
    """Build API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
