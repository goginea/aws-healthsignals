"""CDC Outbreak Alert Dispatch Plugin — State-based delivery for outbreak alerts.

This module is loaded by the alert_dispatcher registry pattern when the
CDC Outbreak Alerts module is enabled. It handles:
- "cdc_outbreak": foodborne/parasitic/bacterial outbreak alerts from CDC

Routes through state-based subscriber lookup: find all active subscribers
in the affected state and deliver via their configured channels.
"""
import json
import logging
from datetime import datetime

from boto3.dynamodb.conditions import Key

logger = logging.getLogger()

# Lazy-initialized references (set by register())
_sub_table = None
_ses = None
_sns = None
_system = None
_api_base_url = None


def register(context: dict) -> dict[str, callable]:
    """Register CDC outbreak dispatch handler with the alert dispatcher.

    Called by the dispatcher registry at module load time.
    Returns a dict mapping alert_type → handler function.

    Args:
        context: Shared context dict with AWS clients and config:
            - sub_table: DynamoDB subscriptions table resource
            - ses: SES client
            - sns: SNS client
            - system: system config dict
            - api_base_url: base URL for unsubscribe links
            - dynamodb: DynamoDB resource
    """
    global _sub_table, _ses, _sns, _system, _api_base_url

    _sub_table = context["sub_table"]
    _ses = context["ses"]
    _sns = context["sns"]
    _system = context["system"]
    _api_base_url = context["api_base_url"]

    return {
        "cdc_outbreak": dispatch_outbreak_alert,
    }


def dispatch_outbreak_alert(event: dict, alert_type: str) -> dict:
    """Deliver CDC outbreak alerts to subscribers in affected states.

    Uses state-based subscription lookup: queries subscriptions table by state
    to find all active, verified, non-paused subscribers in the affected state.

    Input event contains:
    - state_key: the state being alerted
    - counties: list of subscribing counties in that state
    - disease_name, case_count, affected_states, etc.
    - brief_result: Bedrock-generated situation brief
    - severity_result: severity classification
    """
    from shared.token_utils import generate_unsubscribe_url

    state_key = event.get("state_key")
    disease_name = event.get("disease_name", "Unknown Outbreak")
    title = event.get("title", disease_name)
    cdc_link = event.get("cdc_link", "")

    # Extract the generated brief from Bedrock result
    brief_text = ""
    brief_result = event.get("brief_result", {})
    if isinstance(brief_result, dict):
        body = brief_result.get("Body", {})
        content = body.get("content", [{}])
        if content:
            brief_text = content[0].get("text", "")

    # Extract severity
    severity = "MODERATE"
    severity_result = event.get("severity_result", {})
    if isinstance(severity_result, dict):
        body = severity_result.get("Body", {})
        content = body.get("content", [{}])
        if content:
            try:
                sev_json = json.loads(content[0].get("text", "{}"))
                severity = sev_json.get("severity", "MODERATE")
            except (json.JSONDecodeError, TypeError):
                pass

    logger.info(json.dumps({
        "event_type": "outbreak_alert_dispatch_start",
        "state_key": state_key,
        "disease_name": disease_name,
        "severity": severity,
    }))

    if not state_key:
        logger.error("Missing state_key in outbreak alert event")
        return {"error": "Missing state_key", "dispatched": False}

    sender_email = _system["delivery"]["ses_sender_email"]
    max_sms = _system["delivery"]["max_sms_length"]

    # Query subscribers for this state
    subscribers = _get_state_subscribers(state_key)

    results = {"dispatched": [], "skipped": [], "errors": []}

    if not subscribers:
        logger.info(f"No active subscribers in state '{state_key}' for outbreak alert")
        results["skipped"].append({
            "state_key": state_key,
            "reason": "No active subscribers in this state",
        })
    else:
        for sub in subscribers:
            try:
                _dispatch_to_subscriber(
                    sub, brief_text, disease_name, severity, title, cdc_link, sender_email, max_sms
                )
                results["dispatched"].append({
                    "contact": sub.get("contact_email"),
                    "county_fips": sub.get("county_fips"),
                    "channels": sub.get("channels", []),
                })
            except Exception as e:
                logger.error(f"Outbreak alert delivery failed for {sub.get('contact_email')}: {e}")
                results["errors"].append({
                    "contact": sub.get("contact_email"),
                    "error": str(e),
                })

    recipients_count = len(results["dispatched"])

    logger.info(json.dumps({
        "event_type": "outbreak_alert_dispatch_complete",
        "state_key": state_key,
        "disease_name": disease_name,
        "recipients_count": recipients_count,
        "errors_count": len(results["errors"]),
    }))

    return {
        "alert_type": alert_type,
        "state_key": state_key,
        "disease_name": disease_name,
        "severity": severity,
        "total_dispatched": recipients_count,
        "total_errors": len(results["errors"]),
        "details": results,
        "success": recipients_count > 0,
    }


def _get_state_subscribers(state_key: str) -> list:
    """Query subscriptions table for active subscribers in a state.

    Uses the state-index GSI on the subscriptions table.
    Filters: active status, verified, not paused.
    """
    try:
        response = _sub_table.query(
            IndexName="state-index",
            KeyConditionExpression=Key("state").eq(state_key),
        )
        items = response.get("Items", [])
    except Exception as e:
        logger.error(f"Subscription query failed for state={state_key}: {e}")
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

        # Extract channels
        prefs = item.get("delivery_preferences", {})
        channels = prefs.get("channels", ["email"])
        item["channels"] = channels
        eligible.append(item)

    return eligible


def _dispatch_to_subscriber(
    subscriber: dict, brief_text: str, disease_name: str,
    severity: str, title: str, cdc_link: str, sender_email: str, max_sms: int
) -> None:
    """Send outbreak alert to a single subscriber."""
    from shared.token_utils import generate_unsubscribe_url

    channels = subscriber.get("channels", ["email"])
    email = subscriber.get("contact_email")
    phone = subscriber.get("contact_phone") or subscriber.get("phone_number")
    county_fips = subscriber.get("county_fips", "")
    subscription_id = subscriber.get("subscription_id", "")

    unsub_url = generate_unsubscribe_url(_api_base_url, county_fips, subscription_id)

    if "email" in channels and email:
        email_body = brief_text if brief_text else f"CDC Outbreak Alert: {title}. Visit {cdc_link} for details."
        email_body += f"\n\nSource: CDC. Monitor cdc.gov/outbreaks for the latest updates."
        email_body += f"\n\n---\nTo unsubscribe from HealthSignals alerts: {unsub_url}"

        subject = f"[HealthSignals {severity}] CDC Outbreak Alert: {disease_name}"

        _ses.send_email(
            Source=sender_email,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {
                    "Text": {"Data": email_body},
                    "Html": {"Data": f"<pre>{email_body}</pre>"},
                },
            },
        )
        logger.info(f"Outbreak email sent to {email}")

    if "sms" in channels and phone:
        sms_text = f"HealthSignals [{severity}]: {disease_name} outbreak alert. {len(subscriber.get('affected_states', []))} states affected. Check email for details."
        sms_text = sms_text[:max_sms]
        _sns.publish(
            PhoneNumber=phone,
            Message=sms_text,
            MessageAttributes={
                "AWS.SNS.SMS.SenderID": {"DataType": "String", "StringValue": "HealthSig"},
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
            },
        )
        logger.info(f"Outbreak SMS sent to {phone}")
