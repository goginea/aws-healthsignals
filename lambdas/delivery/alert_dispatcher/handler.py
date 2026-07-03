"""Alert Dispatcher — Config-driven delivery via SES/SNS with subscription integration.

Two delivery paths:
1. Config-based: Reads contacts from state config (for counties not using subscription API)
2. Subscription-based: Queries DynamoDB subscriptions table for verified, active subscribers

Both paths coexist — subscription table takes priority if a matching record exists.
Every outbound message includes an unsubscribe link.
"""
import json
import os
import sys
import logging
from datetime import datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key, Attr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import get_system_config
from shared.token_utils import generate_unsubscribe_url

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
SUBSCRIPTIONS_TABLE = os.environ.get(
    "SUBSCRIPTIONS_TABLE",
    system.get("dynamodb_tables", {}).get("subscriptions", "healthsignals-subscriptions")
)
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.healthsignals.example.com/prod")

ses = boto3.client("ses")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")
sub_table = dynamodb.Table(SUBSCRIPTIONS_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Dispatch alerts to health officers via subscriptions + config fallback.

    Input event (from Step Functions — after alert generation):
    {
        "disease": "influenza",
        "severity": "HIGH",
        "county_fips": "48143",
        "county_name": "Erath County",
        "state": "texas",
        "alert_content": {
            "situation_brief": "...",
            "checklist": "...",
            "email_body": "...",
            "sms_text": "..."
        },
        "prediction": {
            "lag_weeks": 4,
            "severity_multiplier": 2.1,
            "confidence": 0.75
        }
    }
    """
    disease = event.get("disease")
    severity = event.get("severity", "MODERATE")
    county_fips = event.get("county_fips")
    county_name = event.get("county_name", "Unknown County")
    alert_content = event.get("alert_content", {})

    if not county_fips:
        return {"error": "Missing county_fips", "dispatched": False}

    # Get delivery config
    sender_email = system["delivery"]["ses_sender_email"]
    max_sms = system["delivery"]["max_sms_length"]

    results = {"dispatched": [], "skipped": [], "errors": []}

    # --- Path 1: Query subscription table ---
    subscribers = _get_active_subscribers(county_fips, disease, severity)

    if subscribers:
        for sub in subscribers:
            try:
                _dispatch_to_subscriber(sub, alert_content, severity, disease, county_name, sender_email, max_sms)
                results["dispatched"].append({
                    "subscription_id": sub["subscription_id"],
                    "channels": sub.get("delivery_preferences", {}).get("channels", []),
                    "contact": sub.get("contact_email"),
                })
                # Update last_alert_sent
                _update_last_alert(county_fips, sub["subscription_id"])
            except Exception as e:
                results["errors"].append({
                    "subscription_id": sub["subscription_id"],
                    "error": str(e),
                })
    else:
        # --- Path 2: Fallback to config-based contacts ---
        logger.info(f"No active subscribers for {county_fips}, using config fallback")
        config_contacts = event.get("county", {}).get("contacts", {})
        if config_contacts:
            try:
                _dispatch_to_config_contact(
                    config_contacts, alert_content, severity, disease, county_name, sender_email, max_sms
                )
                results["dispatched"].append({
                    "source": "config_fallback",
                    "contact": config_contacts.get("health_officer", {}).get("email"),
                })
            except Exception as e:
                results["errors"].append({"source": "config_fallback", "error": str(e)})
        else:
            results["skipped"].append({
                "county_fips": county_fips,
                "reason": "No subscribers and no config contacts found",
            })

    return {
        "county_fips": county_fips,
        "county_name": county_name,
        "disease": disease,
        "severity": severity,
        "total_dispatched": len(results["dispatched"]),
        "total_errors": len(results["errors"]),
        "details": results,
        "success": len(results["dispatched"]) > 0,
    }


def _get_active_subscribers(county_fips: str, disease: str, severity: str) -> list:
    """Query subscriptions table for eligible recipients.

    Filters:
    - status = "active"
    - verified_at is not null
    - disease is in subscription's diseases list
    - severity >= subscription's alert_threshold
    - not currently paused (pause_until is null or in the past)
    """
    try:
        response = sub_table.query(
            KeyConditionExpression=Key("county_fips").eq(county_fips),
        )
        items = response.get("Items", [])
    except Exception as e:
        logger.error(f"Subscription query failed for {county_fips}: {e}")
        return []

    now = datetime.utcnow().isoformat()
    eligible = []

    for item in items:
        # Must be active and verified
        if item.get("status") != "active":
            continue
        if not item.get("verified_at"):
            continue

        # Check pause
        pause_until = item.get("pause_until")
        if pause_until and pause_until > now:
            continue

        # Check disease subscription
        subscribed_diseases = item.get("diseases", [])
        if disease.lower() not in [d.lower() for d in subscribed_diseases]:
            continue

        # Check severity threshold
        prefs = item.get("delivery_preferences", {})
        threshold = prefs.get("alert_threshold", "MODERATE")
        if not _meets_threshold(severity, threshold):
            continue

        eligible.append(item)

    return eligible


def _dispatch_to_subscriber(
    subscriber: dict, alert_content: dict, severity: str,
    disease: str, county_name: str, sender_email: str, max_sms: int
) -> None:
    """Send alert to a single subscriber via their preferred channels."""
    prefs = subscriber.get("delivery_preferences", {})
    channels = prefs.get("channels", ["email"])
    email = subscriber.get("contact_email")
    phone = subscriber.get("contact_phone")
    subscription_id = subscriber["subscription_id"]
    county_fips = subscriber["county_fips"]

    # Generate unsubscribe URL for this subscriber
    unsub_url = generate_unsubscribe_url(API_BASE_URL, county_fips, subscription_id)

    if "email" in channels and email:
        email_body = alert_content.get("email_body", alert_content.get("situation_brief", ""))
        # Append unsubscribe footer
        email_body += f"\n\n---\nTo unsubscribe from HealthSignals alerts: {unsub_url}"

        _send_email(
            sender=sender_email,
            recipient=email,
            subject=f"[HealthSignals {severity}] {disease.title()} Alert — {county_name}",
            body=email_body,
        )

    if "sms" in channels and phone:
        sms_text = alert_content.get("sms_text", "")[:max_sms]
        _send_sms(phone_number=phone, message=sms_text)


def _dispatch_to_config_contact(
    contacts: dict, alert_content: dict, severity: str,
    disease: str, county_name: str, sender_email: str, max_sms: int
) -> None:
    """Fallback: send alert using config-based contacts (no subscription)."""
    officer = contacts.get("health_officer", {})
    email = officer.get("email")
    phone = officer.get("phone")

    if email:
        email_body = alert_content.get("email_body", alert_content.get("situation_brief", ""))
        _send_email(
            sender=sender_email,
            recipient=email,
            subject=f"[HealthSignals {severity}] {disease.title()} Alert — {county_name}",
            body=email_body,
        )

    if phone:
        sms_text = alert_content.get("sms_text", "")[:max_sms]
        _send_sms(phone_number=phone, message=sms_text)


def _update_last_alert(county_fips: str, subscription_id: str) -> None:
    """Record last alert timestamp on subscription record."""
    try:
        sub_table.update_item(
            Key={"county_fips": county_fips, "subscription_id": subscription_id},
            UpdateExpression="SET last_alert_sent = :now",
            ExpressionAttributeValues={":now": datetime.utcnow().isoformat()},
        )
    except Exception as e:
        logger.warning(f"Failed to update last_alert_sent: {e}")


# --- Severity comparison ---
SEVERITY_ORDER = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def _meets_threshold(alert_severity: str, min_threshold: str) -> bool:
    """Check if alert severity meets the subscriber's minimum threshold."""
    alert_level = SEVERITY_ORDER.get(alert_severity.upper(), 0)
    min_level = SEVERITY_ORDER.get(min_threshold.upper(), 0)
    return alert_level >= min_level


# --- Delivery methods ---
def _send_email(sender: str, recipient: str, subject: str, body: str) -> None:
    """Send alert email via SES."""
    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Text": {"Data": body},
                "Html": {"Data": f"<pre>{body}</pre>"},
            },
        },
    )
    logger.info(f"Email sent to {recipient}")


def _send_sms(phone_number: str, message: str) -> None:
    """Send alert SMS via SNS."""
    sns.publish(
        PhoneNumber=phone_number,
        Message=message,
        MessageAttributes={
            "AWS.SNS.SMS.SenderID": {"DataType": "String", "StringValue": "HealthSig"},
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
        },
    )
    logger.info(f"SMS sent to {phone_number}")
