"""Subscribe Handler — Creates a new county subscription for HealthSignals alerts.

POST /subscription/subscribe
Double opt-in: subscription starts as "pending_verification" until email is confirmed.
"""
import json
import os
import sys
import re
import uuid
import logging
from datetime import datetime
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
from config_loader import get_system_config, list_active_diseases, list_active_states
from token_utils import generate_verification_url

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
SUBSCRIPTIONS_TABLE = os.environ.get(
    "SUBSCRIPTIONS_TABLE", system.get("dynamodb_tables", {}).get("subscriptions", "healthsignals-subscriptions")
)
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.healthsignals.example.com/prod")
SENDER_EMAIL = system.get("delivery", {}).get("ses_sender_email", "alerts@healthsignals.example.com")
MAX_SUBSCRIPTIONS_PER_COUNTY = int(os.environ.get("MAX_SUBSCRIPTIONS_PER_COUNTY", "10"))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(SUBSCRIPTIONS_TABLE)
ses = boto3.client("ses")


def lambda_handler(event: dict, context: Any) -> dict:
    """Create a new county subscription.

    Input (API Gateway proxy event):
    {
        "body": {
            "county_fips": "48143",
            "county_name": "Erath County",
            "state": "texas",
            "contact_name": "Dr. Jane Smith",
            "contact_email": "jane.smith@erathcounty.gov",
            "contact_phone": "+15551234567",   // optional
            "diseases": ["influenza", "rsv", "covid"],  // optional, defaults to all active
            "delivery_preferences": {
                "channels": ["email", "sms"],   // optional, defaults to ["email"]
                "alert_threshold": "MODERATE",  // optional, minimum severity to alert
                "quiet_hours": null             // optional, e.g., "22:00-07:00"
            }
        }
    }
    """
    try:
        body = _parse_body(event)
        _validate_input(body)

        # Check subscription limits
        existing = _count_active_subscriptions(body["county_fips"])
        if existing >= MAX_SUBSCRIPTIONS_PER_COUNTY:
            return _response(409, {
                "error": "subscription_limit_reached",
                "message": f"Maximum {MAX_SUBSCRIPTIONS_PER_COUNTY} active subscriptions per county.",
                "current_count": existing,
            })

        # Generate subscription ID and tokens
        subscription_id = str(uuid.uuid4())

        # Build subscription record
        now = datetime.utcnow().isoformat()
        diseases = body.get("diseases") or list_active_diseases()
        delivery_prefs = body.get("delivery_preferences", {})

        record = {
            "county_fips": body["county_fips"],
            "subscription_id": subscription_id,
            "county_name": body["county_name"],
            "state": body["state"],
            "contact_name": body["contact_name"],
            "contact_email": body["contact_email"],
            "contact_phone": body.get("contact_phone", ""),
            "diseases": diseases,
            "delivery_preferences": {
                "channels": delivery_prefs.get("channels", ["email"]),
                "alert_threshold": delivery_prefs.get("alert_threshold", "MODERATE"),
                "quiet_hours": delivery_prefs.get("quiet_hours"),
            },
            "status": "pending_verification",
            "created_at": now,
            "updated_at": now,
            "verified_at": None,
            "last_alert_sent": None,
            "pause_until": None,
            "metadata": {
                "source": "api",
                "ip": event.get("requestContext", {}).get("identity", {}).get("sourceIp", "unknown"),
            },
        }

        # Write to DynamoDB
        table.put_item(Item=_clean_for_dynamo(record))

        # Send verification email
        verification_url = generate_verification_url(API_BASE_URL, body["county_fips"], subscription_id)
        _send_verification_email(body["contact_email"], body["contact_name"], body["county_name"], verification_url)

        logger.info(f"Subscription created: {subscription_id} for {body['county_name']} ({body['county_fips']})")

        return _response(201, {
            "subscription_id": subscription_id,
            "county_fips": body["county_fips"],
            "status": "pending_verification",
            "message": "Subscription created. Please check your email to verify.",
            "diseases": diseases,
        })

    except ValidationError as e:
        return _response(400, {"error": "validation_error", "message": str(e)})
    except Exception as e:
        logger.error(f"Subscription creation failed: {e}", exc_info=True)
        return _response(500, {"error": "internal_error", "message": "Failed to create subscription."})


def _parse_body(event: dict) -> dict:
    """Parse request body from API Gateway event."""
    body = event.get("body", "")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise ValidationError("Invalid JSON in request body")
    return body or {}


def _validate_input(body: dict) -> None:
    """Validate required fields and formats."""
    # Required fields
    required = ["county_fips", "county_name", "state", "contact_name", "contact_email"]
    for field in required:
        if not body.get(field):
            raise ValidationError(f"Missing required field: {field}")

    # FIPS format: 5 digits
    if not re.match(r"^\d{5}$", body["county_fips"]):
        raise ValidationError(f"Invalid county_fips format: must be 5 digits (got '{body['county_fips']}')")

    # Email format
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", body["contact_email"]):
        raise ValidationError(f"Invalid email format: {body['contact_email']}")

    # Phone format (optional, but if provided must be E.164)
    if body.get("contact_phone"):
        if not re.match(r"^\+\d{10,15}$", body["contact_phone"]):
            raise ValidationError(f"Invalid phone format: must be E.164 (e.g., +15551234567)")

    # State must exist in config
    active_states = list_active_states()
    if body["state"].lower() not in [s.lower() for s in active_states]:
        raise ValidationError(f"State '{body['state']}' not configured. Active: {active_states}")

    # Diseases must be valid if specified
    if body.get("diseases"):
        active_diseases = list_active_diseases()
        for d in body["diseases"]:
            if d.lower() not in [ad.lower() for ad in active_diseases]:
                raise ValidationError(f"Disease '{d}' not configured. Active: {active_diseases}")


def _count_active_subscriptions(county_fips: str) -> int:
    """Count active subscriptions for a county (abuse prevention)."""
    try:
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("county_fips").eq(county_fips),
            FilterExpression=boto3.dynamodb.conditions.Attr("status").is_in(
                ["active", "pending_verification"]
            ),
            Select="COUNT",
        )
        return response.get("Count", 0)
    except Exception:
        return 0  # Fail open — don't block subscription on count errors


def _send_verification_email(email: str, name: str, county: str, verification_url: str) -> None:
    """Send double opt-in verification email."""
    subject = f"Verify your HealthSignals subscription — {county}"
    body = f"""Hello {name},

Thank you for subscribing {county} to Amazon HealthSignals disease preparedness alerts.

To activate your subscription, please verify your email by clicking the link below:

{verification_url}

This link expires in 72 hours.

What you'll receive:
- Weekly situation briefs when respiratory disease activity is detected in sentinel metro areas
- Preparation checklists with estimated arrival timing for your county
- SMS alerts for high-severity situations (if you provided a phone number)

If you did not request this subscription, you can safely ignore this email.

—
Amazon HealthSignals
Predictive Disease Surveillance for Rural Communities
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
        logger.error(f"Failed to send verification email to {email}: {e}")
        # Don't raise — subscription is still created, they can request resend


def _clean_for_dynamo(record: dict) -> dict:
    """Remove None values (DynamoDB doesn't accept None for non-existent attributes)."""
    return {k: v for k, v in record.items() if v is not None}


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


class ValidationError(Exception):
    """Raised for input validation failures."""
    pass
