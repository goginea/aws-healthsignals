"""Alert Dispatcher — Registry-based delivery routing for HealthSignals alerts.

Uses a plugin registry pattern: each alert_type maps to a handler function.
The core module registers "disease_outbreak". Plugin modules register their own
handlers by placing a module in this directory with a register(context) function
that returns {alert_type: handler_fn}.

Delivery paths:
1. disease_outbreak: County-based subscription delivery (core)
2. Plugin-registered types: Each plugin owns its routing logic

Plugin modules are auto-discovered from DISPATCH_PLUGINS env var (comma-separated
module names relative to this package, e.g., "shortage_dispatch").
"""
import json
import os
import sys
import logging
import importlib
from datetime import datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

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
dynamodb_client = boto3.client("dynamodb")
sub_table = dynamodb.Table(SUBSCRIPTIONS_TABLE)

# === Dispatch Handler Registry ===
# Maps alert_type → handler function. Core registers "disease_outbreak".
# Plugins register additional types via their register() function.
_dispatch_registry: dict[str, callable] = {}


def _register_core_handlers() -> None:
    """Register the core disease_outbreak handler."""
    _dispatch_registry["disease_outbreak"] = _dispatch_disease_outbreak_alert


def _load_plugin_handlers() -> None:
    """Auto-discover and load plugin dispatch modules.

    Reads DISPATCH_PLUGINS env var (comma-separated module names).
    Each module must expose a register(context) function returning
    {alert_type: handler_fn}.

    Example: DISPATCH_PLUGINS=shortage_dispatch
    """
    plugins_str = os.environ.get("DISPATCH_PLUGINS", "")
    if not plugins_str:
        return

    # Build shared context for plugins
    context = {
        "sub_table": sub_table,
        "ses": ses,
        "sns": sns,
        "system": system,
        "api_base_url": API_BASE_URL,
        "dynamodb": dynamodb,
    }

    for plugin_name in plugins_str.split(","):
        plugin_name = plugin_name.strip()
        if not plugin_name:
            continue
        try:
            # Import plugin module relative to this package directory
            plugin_module = importlib.import_module(f".{plugin_name}", package=__package__)
            if hasattr(plugin_module, "register"):
                handlers = plugin_module.register(context)
                for alert_type, handler_fn in handlers.items():
                    _dispatch_registry[alert_type] = handler_fn
                    logger.info(f"Registered dispatch plugin: {plugin_name} → alert_type={alert_type}")
            else:
                logger.warning(f"Plugin {plugin_name} has no register() function, skipping")
        except Exception as e:
            logger.error(f"Failed to load dispatch plugin '{plugin_name}': {e}")


# Initialize registry at module load time (after all handlers are defined at bottom of file)
# See end of file for: _register_core_handlers() and _load_plugin_handlers()


def lambda_handler(event: dict, context: Any) -> dict:
    """Dispatch alerts using the registered handler for the event's alert_type.

    Input event (from Step Functions — after alert generation):
    {
        "alert_type": "disease_outbreak" | "<plugin_type>",
        "disease": "influenza",
        "county_fips": "48143",
        "alert_content": {...},
        ...
    }
    """
    alert_type = event.get("alert_type", "disease_outbreak")

    handler_fn = _dispatch_registry.get(alert_type)

    if handler_fn is None:
        logger.error(f"No handler registered for alert_type='{alert_type}'. "
                     f"Registered types: {list(_dispatch_registry.keys())}")
        return {
            "error": f"Unknown alert_type: {alert_type}",
            "registered_types": list(_dispatch_registry.keys()),
            "dispatched": False,
        }

    # Core handler takes (event), plugin handlers take (event, alert_type)
    if alert_type == "disease_outbreak":
        return handler_fn(event)
    else:
        return handler_fn(event, alert_type)


# === Core Disease Outbreak Dispatch ===


def _extract_alert_content(event: dict) -> dict:
    """Extract email_body and sms_text from the SFN state.

    The Step Functions workflow stores Bedrock output at:
      communication_result.Body.content[0].text
    which contains both EMAIL and SMS sections generated by the
    CommunicationDrafting prompt.

    Also extracts severity from severity_result if not at top level.
    """
    alert_content = event.get("alert_content")
    if alert_content and alert_content.get("email_body"):
        # Already structured (e.g., from a plugin or pre-processed event)
        return alert_content

    # Extract from Bedrock communication_result
    comm = event.get("communication_result", {})
    body_content = comm.get("Body", {}).get("content", [])
    full_text = body_content[0].get("text", "") if body_content else ""

    if not full_text:
        # Fallback: try situation_brief_result directly
        brief = event.get("situation_brief_result", {})
        brief_content = brief.get("Body", {}).get("content", [])
        full_text = brief_content[0].get("text", "") if brief_content else ""

    # Parse EMAIL and SMS sections from the communication draft
    email_body = full_text
    sms_text = ""

    # Common patterns from the CommunicationDrafting prompt output:
    # "# OUTPUT 1: EMAIL BRIEF" ... "# OUTPUT 2: SMS ALERT"
    # or "## EMAIL" ... "## SMS"
    sms_markers = ["# OUTPUT 2: SMS", "## SMS ALERT", "## OUTPUT 2:", "# SMS ALERT", "---\n**SMS"]
    for marker in sms_markers:
        if marker in full_text:
            parts = full_text.split(marker, 1)
            email_body = parts[0].strip()
            sms_text = parts[1].strip()
            # Clean up SMS — extract just the message content (strip markdown headers)
            sms_lines = [l for l in sms_text.split("\n") if l.strip() and not l.startswith("#")]
            sms_text = sms_lines[0] if sms_lines else sms_text[:160]
            break

    # Trim SMS to 160 chars
    sms_text = sms_text[:160]

    return {"email_body": email_body, "sms_text": sms_text}


def _extract_severity(event: dict) -> str:
    """Extract severity from SFN state — may be at top level or inside severity_result."""
    severity = event.get("severity")
    if severity:
        return severity

    # Parse from Bedrock severity_result
    sev_result = event.get("severity_result", {})
    sev_content = sev_result.get("Body", {}).get("content", [])
    sev_text = sev_content[0].get("text", "") if sev_content else ""

    if sev_text:
        import re
        match = re.search(r'"severity"\s*:\s*"(LOW|MODERATE|HIGH|CRITICAL)"', sev_text)
        if match:
            return match.group(1)

    return "MODERATE"


def _dispatch_disease_outbreak_alert(event: dict) -> dict:
    """Core disease outbreak alert delivery — county-based subscription lookup."""
    disease = event.get("disease")
    county_fips = event.get("county_fips")
    county_name = event.get("county_name", "Unknown County")

    # Extract structured content from SFN Bedrock results
    alert_content = _extract_alert_content(event)
    severity = _extract_severity(event)

    if not county_fips:
        return {"error": "Missing county_fips", "dispatched": False}

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


# === Subscriber Query & Delivery ===


def _get_active_subscribers(county_fips: str, disease: str, severity: str) -> list:
    """Query subscriptions table for eligible recipients."""
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
        if item.get("status") != "active":
            continue
        if not item.get("verified_at"):
            continue
        pause_until = item.get("pause_until")
        if pause_until and pause_until > now:
            continue
        subscribed_diseases = item.get("diseases", [])
        if disease.lower() not in [d.lower() for d in subscribed_diseases]:
            continue
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

    unsub_url = generate_unsubscribe_url(API_BASE_URL, county_fips, subscription_id)

    if "email" in channels and email:
        email_body = alert_content.get("email_body", alert_content.get("situation_brief", ""))
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


# === Severity comparison ===
SEVERITY_ORDER = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def _meets_threshold(alert_severity: str, min_threshold: str) -> bool:
    """Check if alert severity meets the subscriber's minimum threshold."""
    alert_level = SEVERITY_ORDER.get(alert_severity.upper(), 0)
    min_level = SEVERITY_ORDER.get(min_threshold.upper(), 0)
    return alert_level >= min_level


# === Delivery methods ===
def _markdown_to_html(md: str) -> str:
    """Convert Bedrock-generated markdown to HTML for email rendering.

    Handles: headers, bold, italic, bullet lists, horizontal rules, tables,
    and paragraphs. Lightweight — no external dependencies needed.
    """
    import re

    lines = md.split("\n")
    html_lines = []
    in_list = False
    in_table = False

    for line in lines:
        stripped = line.strip()

        # Horizontal rule
        if stripped == "---" or stripped == "***":
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_table:
                html_lines.append("</table>")
                in_table = False
            html_lines.append("<hr>")
            continue

        # Table rows (detect by | separators)
        if "|" in stripped and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Skip separator rows (|---|---|)
            if all(c.replace("-", "").replace(":", "") == "" for c in cells):
                continue
            if not in_table:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append('<table style="border-collapse:collapse;width:100%;margin:10px 0">')
                # First table row is header
                html_lines.append("<tr>" + "".join(
                    f'<th style="border:1px solid #ddd;padding:8px;background:#f4f4f4;text-align:left">{c}</th>'
                    for c in cells
                ) + "</tr>")
                in_table = True
            else:
                html_lines.append("<tr>" + "".join(
                    f'<td style="border:1px solid #ddd;padding:8px">{c}</td>'
                    for c in cells
                ) + "</tr>")
            continue

        if in_table and not ("|" in stripped):
            html_lines.append("</table>")
            in_table = False

        # Headers
        if stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f'<h3 style="color:#333;margin:16px 0 8px">{stripped[4:]}</h3>')
            continue
        if stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f'<h2 style="color:#1a5276;margin:20px 0 10px;border-bottom:1px solid #eee;padding-bottom:5px">{stripped[3:]}</h2>')
            continue
        if stripped.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f'<h1 style="color:#154360;margin:24px 0 12px">{stripped[2:]}</h1>')
            continue

        # Bullet lists (- or * at start)
        if re.match(r"^[-*]\s", stripped):
            if not in_list:
                html_lines.append('<ul style="margin:8px 0;padding-left:24px">')
                in_list = True
            content = stripped[2:]
            html_lines.append(f"<li>{content}</li>")
            continue

        # Indented bullets (  - or    -)
        indent_match = re.match(r"^(\s+)[-*]\s(.+)", line)
        if indent_match:
            if not in_list:
                html_lines.append('<ul style="margin:8px 0;padding-left:24px">')
                in_list = True
            content = indent_match.group(2)
            html_lines.append(f'<li style="margin-left:16px">{content}</li>')
            continue

        # Non-list line closes any open list
        if in_list and stripped:
            html_lines.append("</ul>")
            in_list = False

        # Empty line = paragraph break
        if not stripped:
            html_lines.append("<br>")
            continue

        # Regular paragraph
        html_lines.append(f"<p style='margin:6px 0'>{stripped}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_table:
        html_lines.append("</table>")

    html_body = "\n".join(html_lines)

    # Inline formatting: **bold**, *italic*, `code`
    html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
    html_body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html_body)
    html_body = re.sub(r"`(.+?)`", r'<code style="background:#f5f5f5;padding:2px 4px;border-radius:3px">\1</code>', html_body)

    # Wrap in email template
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;line-height:1.6">
{html_body}
</div>"""


def _send_email(sender: str, recipient: str, subject: str, body: str) -> None:
    """Send alert email via SES."""
    html_body = _markdown_to_html(body)
    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Text": {"Data": body},
                "Html": {"Data": html_body},
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


# === Initialize Registry (must be after all handler functions are defined) ===
_register_core_handlers()
_load_plugin_handlers()
