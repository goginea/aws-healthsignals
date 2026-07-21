"""Shortage Alert Dispatch Plugin — Therapeutic category-based delivery for drug shortage alerts.

This module is loaded by the alert_dispatcher registry pattern when the
Drug Shortage Intelligence module is enabled. It handles:
- "shortage": standalone drug shortage alerts
- "combined": disease outbreak + shortage context alerts

Both route through therapeutic-category-based subscriber lookup.
"""
import json
import logging
from datetime import datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from .handler import _markdown_to_html

logger = logging.getLogger()

# Lazy-initialized table reference (set by register())
_shortage_alerts_table = None
_sub_table = None
_ses = None
_sns = None
_system = None
_api_base_url = None


def register(context: dict) -> dict[str, callable]:
    """Register shortage dispatch handlers with the alert dispatcher.

    Called by the dispatcher registry at module load time.
    Returns a dict mapping alert_type → handler function.

    Args:
        context: Shared context dict with AWS clients and config:
            - sub_table: DynamoDB subscriptions table resource
            - ses: SES client
            - sns: SNS client
            - system: system config dict
            - api_base_url: base URL for unsubscribe links
            - dynamodb: DynamoDB resource (for additional tables)
    """
    global _shortage_alerts_table, _sub_table, _ses, _sns, _system, _api_base_url

    _sub_table = context["sub_table"]
    _ses = context["ses"]
    _sns = context["sns"]
    _system = context["system"]
    _api_base_url = context["api_base_url"]

    # Initialize shortage-specific table
    import os
    shortage_table_name = os.environ.get(
        "SHORTAGE_ALERTS_TABLE",
        os.environ.get("PLUGIN_ALERTS_TABLE", ""),
    )
    if shortage_table_name:
        dynamodb_resource = context["dynamodb"]
        _shortage_alerts_table = dynamodb_resource.Table(shortage_table_name)

    return {
        "shortage": dispatch_shortage_alert,
        "combined": dispatch_shortage_alert,
    }


def dispatch_shortage_alert(event: dict, alert_type: str) -> dict:
    """Deliver shortage or combined alerts filtered by therapeutic category subscription.

    Queries the subscriptions table GSI `therapeutic-category-lookup` for subscribers
    matching the alert's therapeutic_category. Filters for active, verified, non-paused
    subscriptions. Sends SES email and optionally SNS SMS to each matching subscriber.
    Updates the shortage-alerts table record to SENT on success.
    """
    from shared.token_utils import generate_unsubscribe_url

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

    sender_email = _system["delivery"]["ses_sender_email"]
    max_sms = _system["delivery"]["max_sms_length"]

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
    """Query subscriptions table using GSI therapeutic-category-lookup."""
    try:
        response = _sub_table.query(
            IndexName="alert-category-lookup",
            KeyConditionExpression=Key("alert_category").eq(therapeutic_category),
        )
        items = response.get("Items", [])
    except Exception as e:
        logger.error(f"Subscription GSI query failed for therapeutic_category={therapeutic_category}: {e}")
        return []

    now = datetime.utcnow().isoformat()
    eligible = []

    for item in items:
        if item.get("status") != "active":
            continue
        if not item.get("verified_at"):
            continue
        pause_until = item.get("pause_until")
        if pause_until and pause_until > now:
            continue
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
    from shared.token_utils import generate_unsubscribe_url

    channels = subscriber.get("channels", ["email"])
    email = subscriber.get("contact_email")
    phone = subscriber.get("contact_phone") or subscriber.get("phone_number")
    county_fips = subscriber.get("county_fips", "")
    subscription_id = subscriber.get("subscription_id", "")

    unsub_url = generate_unsubscribe_url(_api_base_url, county_fips, subscription_id)

    if "email" in channels and email:
        email_body = alert_content.get("email_body", alert_content.get("situation_brief", ""))
        disclaimer = "\n\nFOR PHARMACIST REVIEW ONLY — No specific drug substitution recommendations provided."
        email_body += disclaimer
        email_body += f"\n\n---\nTo unsubscribe from HealthSignals alerts: {unsub_url}"
        subject = f"[HealthSignals] Drug Shortage Alert: {therapeutic_category}"

        _ses.send_email(
            Source=sender_email,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {
                    "Text": {"Data": email_body},
                    "Html": {"Data": _markdown_to_html(email_body)},
                },
            },
        )
        logger.info(f"Shortage email sent to {email}")

    if "sms" in channels and phone:
        sms_text = alert_content.get("sms_text", "")
        if not sms_text:
            sms_text = f"HealthSignals: Drug shortage alert for {therapeutic_category}. Check email for details."
        sms_text = sms_text[:max_sms]
        _sns.publish(
            PhoneNumber=phone,
            Message=sms_text,
            MessageAttributes={
                "AWS.SNS.SMS.SenderID": {"DataType": "String", "StringValue": "HealthSig"},
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
            },
        )
        logger.info(f"Shortage SMS sent to {phone}")


def _update_shortage_alert_status(product_id: str, week_timestamp: str, recipients_count: int) -> None:
    """Update the shortage-alerts table record to status=SENT."""
    if not _shortage_alerts_table:
        return
    try:
        _shortage_alerts_table.update_item(
            Key={"product_id": product_id, "week_timestamp": week_timestamp},
            UpdateExpression="SET alert_generated = :status, delivery_timestamp = :ts, recipients_count = :count",
            ExpressionAttributeValues={
                ":status": "SENT",
                ":ts": datetime.utcnow().isoformat(),
                ":count": recipients_count,
            },
        )
    except Exception as e:
        logger.error(f"Failed to update shortage alert status: product_id={product_id}, week={week_timestamp}: {e}")
