"""Alert Dispatcher — Config-driven delivery via SES/SNS with subscription integration.

Two delivery paths:
1. Config-based: Reads contacts from state config (for counties not using subscription API)
2. Subscription-based: Queries DynamoDB subscriptions table for verified, active subscribers

Both paths coexist — subscription table takes priority if a matching record exists.
Every outbound message includes an unsubscribe link.

Shortage alert delivery (added for Drug Shortage Intelligence Module):
3. Shortage/Combined: Queries subscriptions via therapeutic-category-lookup GSI
   and dispatches shortage alerts to matching subscribers.
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
SHORTAGE_ALERTS_TABLE = os.environ.get(
    "SHORTAGE_ALERTS_TABLE",
    system.get("dynamodb_tables", {}).get("shortage_alerts", "healthsignals-shortage-alerts")
)
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.healthsignals.example.com/prod")

ses = boto3.client("ses")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")
dynamodb_client = boto3.client("dynamodb")
sub_table = dynamodb.Table(SUBSCRIPTIONS_TABLE)
shortage_alerts_table = dynamodb.Table(SHORTAGE_ALERTS_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Dispatch alerts to health officers via subscriptions + config fallback.

    Routes based on alert_type:
    - "disease_outbreak" (default): existing county-based subscription delivery
    - "shortage": therapeutic category-based delivery for drug shortage alerts
    - "combined": therapeutic category-based delivery for combined disease+shortage alerts

    Input event (from Step Functions — after alert generation):
    {
        "disease": "influenza",
        "severity": "HIGH",
        "county_fips": "48143",
        "county_name": "Erath County",
        "state": "texas",
        "alert_type": "disease_outbreak" | "shortage" | "combined",
        "therapeutic_category": "Antivirals",  // for shortage/combined
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
    alert_type = event.get("alert_type", "disease_outbreak")

    # Route shortage and combined alerts to the therapeutic category delivery path
    if alert_type in ("shortage", "combined"):
        return _dispatch_shortage_alert(event, alert_type)

    # Default: disease_outbreak path (existing logic)
    return _dispatch_disease_outbreak_alert(event)


def _dispatch_disease_outbreak_alert(event: dict) -> dict:
    """Existing disease outbreak alert delivery logic (unchanged)."""
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


# --- Shortage Alert Delivery ---


def _dispatch_shortage_alert(event: dict, alert_type: str) -> dict:
    """Deliver shortage or combined alerts filtered by therapeutic category subscription.

    Queries the subscriptions table GSI `therapeutic-category-lookup` for subscribers
    matching the alert's therapeutic_category. Filters for active, verified, non-paused
    subscriptions. Sends SES email and optionally SNS SMS to each matching subscriber.
    Updates the shortage-alerts table record to SENT on success.
    """
    therapeutic_category = event.get("therapeutic_category")
    alert_content = event.get("alert_content", {})
    product_id = event.get("product_id")
    week_timestamp = event.get("week_timestamp")

    logger.info(json.dumps({
        "event_type": "shortage_alert_dispatch_start",
        "alert_type": alert_type,
        "therapeutic_category": therapeutic_category,
        "product_id": product_id,
        "week_timestamp": week_timestamp,
    }))

    if not therapeutic_category:
        logger.error("Missing therapeutic_category in shortage alert event")
        return {"error": "Missing therapeutic_category", "dispatched": False}

    sender_email = system["delivery"]["ses_sender_email"]
    max_sms = system["delivery"]["max_sms_length"]

    # Query subscriptions via therapeutic-category-lookup GSI
    subscribers = _get_shortage_subscribers(therapeutic_category)

    results = {"dispatched": [], "skipped": [], "errors": []}

    if not subscribers:
        logger.info(json.dumps({
            "event_type": "shortage_alert_no_subscribers",
            "therapeutic_category": therapeutic_category,
        }))
        results["skipped"].append({
            "therapeutic_category": therapeutic_category,
            "reason": "No active subscribers for therapeutic category",
        })
    else:
        for sub in subscribers:
            try:
                _dispatch_shortage_to_subscriber(
                    sub, alert_content, therapeutic_category, alert_type, sender_email, max_sms
                )
                results["dispatched"].append({
                    "contact": sub.get("contact_email"),
                    "county_fips": sub.get("county_fips"),
                    "channels": sub.get("channels", []),
                })
            except Exception as e:
                logger.error(f"Shortage alert delivery failed for {sub.get('contact_email')}: {e}")
                results["errors"].append({
                    "contact": sub.get("contact_email"),
                    "error": str(e),
                })

    recipients_count = len(results["dispatched"])

    # Update shortage-alerts table record to SENT
    if product_id and week_timestamp and recipients_count > 0:
        _update_shortage_alert_status(product_id, week_timestamp, recipients_count)

    logger.info(json.dumps({
        "event_type": "shortage_alert_dispatch_complete",
        "alert_type": alert_type,
        "therapeutic_category": therapeutic_category,
        "recipients_count": recipients_count,
        "errors_count": len(results["errors"]),
    }))

    return {
        "alert_type": alert_type,
        "therapeutic_category": therapeutic_category,
        "total_dispatched": recipients_count,
        "total_errors": len(results["errors"]),
        "details": results,
        "success": recipients_count > 0,
    }


def _get_shortage_subscribers(therapeutic_category: str) -> list:
    """Query subscriptions table using GSI therapeutic-category-lookup.

    Filters:
    - status = "active"
    - verified (verified_at is not null)
    - not currently paused (pause_until is null or in the past)
    """
    try:
        response = sub_table.query(
            IndexName="therapeutic-category-lookup",
            KeyConditionExpression=Key("therapeutic_category").eq(therapeutic_category),
        )
        items = response.get("Items", [])
    except Exception as e:
        logger.error(f"Subscription GSI query failed for therapeutic_category={therapeutic_category}: {e}")
        return []

    now = datetime.utcnow().isoformat()
    eligible = []

    for item in items:
        # Must be active
        if item.get("status") != "active":
            continue

        # Must be verified
        if not item.get("verified_at"):
            continue

        # Check pause
        pause_until = item.get("pause_until")
        if pause_until and pause_until > now:
            continue

        # Extract channels from delivery_preferences or top-level
        prefs = item.get("delivery_preferences", {})
        channels = prefs.get("channels", item.get("channels", ["email"]))
        item["channels"] = channels

        eligible.append(item)

    return eligible


def _dispatch_shortage_to_subscriber(
    subscriber: dict, alert_content: dict, therapeutic_category: str,
    alert_type: str, sender_email: str, max_sms: int
) -> None:
    """Send shortage/combined alert to a single subscriber."""
    channels = subscriber.get("channels", ["email"])
    email = subscriber.get("contact_email")
    phone = subscriber.get("contact_phone") or subscriber.get("phone_number")
    county_fips = subscriber.get("county_fips", "")
    subscription_id = subscriber.get("subscription_id", "")

    # Generate unsubscribe URL
    unsub_url = generate_unsubscribe_url(API_BASE_URL, county_fips, subscription_id)

    if "email" in channels and email:
        email_body = alert_content.get("email_body", alert_content.get("situation_brief", ""))

        # Add pharmacist disclaimer
        disclaimer = "\n\nFOR PHARMACIST REVIEW ONLY — No specific drug substitution recommendations provided."
        email_body += disclaimer

        # Add unsubscribe footer
        email_body += f"\n\n---\nTo unsubscribe from HealthSignals alerts: {unsub_url}"

        subject = f"[HealthSignals] Drug Shortage Alert: {therapeutic_category}"

        _send_email(
            sender=sender_email,
            recipient=email,
            subject=subject,
            body=email_body,
        )

    if "sms" in channels and phone:
        sms_text = alert_content.get("sms_text", "")
        if not sms_text:
            sms_text = f"HealthSignals: Drug shortage alert for {therapeutic_category}. Check email for details."
        # Truncate to max SMS length (<=160 chars)
        sms_text = sms_text[:max_sms]
        _send_sms(phone_number=phone, message=sms_text)


def _update_shortage_alert_status(product_id: str, week_timestamp: str, recipients_count: int) -> None:
    """Update the shortage-alerts table record to status=SENT with delivery details."""
    try:
        shortage_alerts_table.update_item(
            Key={"product_id": product_id, "week_timestamp": week_timestamp},
            UpdateExpression="SET alert_generated = :status, delivery_timestamp = :ts, recipients_count = :count",
            ExpressionAttributeValues={
                ":status": "SENT",
                ":ts": datetime.utcnow().isoformat(),
                ":count": recipients_count,
            },
        )
        logger.info(json.dumps({
            "event_type": "shortage_alert_status_updated",
            "product_id": product_id,
            "week_timestamp": week_timestamp,
            "status": "SENT",
            "recipients_count": recipients_count,
        }))
    except Exception as e:
        logger.error(f"Failed to update shortage alert status: product_id={product_id}, week={week_timestamp}: {e}")


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
