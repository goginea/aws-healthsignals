"""Update Preferences Handler — Modify subscription settings.

PUT /subscription/update_preferences
Supports: changing contact info, diseases, delivery channels, pause/resume,
and therapeutic category subscriptions for drug shortage alerts.
"""
import json
import os
import sys
import re
import logging
from datetime import datetime
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
from config_loader import get_system_config, list_active_diseases

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
SUBSCRIPTIONS_TABLE = os.environ.get(
    "SUBSCRIPTIONS_TABLE", system.get("dynamodb_tables", {}).get("subscriptions", "healthsignals-subscriptions")
)
CONFIG_BUCKET = os.environ.get(
    "CONFIG_BUCKET", system.get("config_bucket", f"healthsignals-data-{os.environ.get('AWS_ACCOUNT_ID', 'unknown')}-{os.environ.get('AWS_REGION', 'us-east-1')}")
)
CONFIG_PREFIX = os.environ.get("CONFIG_PREFIX", "config/")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(SUBSCRIPTIONS_TABLE)
s3_client = boto3.client("s3")

# Fields that can be updated
UPDATABLE_FIELDS = {
    "contact_name", "contact_email", "contact_phone",
    "diseases", "delivery_preferences", "pause_until",
    "therapeutic_categories",
}


def lambda_handler(event: dict, context: Any) -> dict:
    """Update subscription preferences.

    Input body:
    {
        "county_fips": "48143",
        "subscription_id": "uuid-...",
        "updates": {
            "contact_email": "new.email@county.gov",
            "diseases": ["influenza", "rsv"],
            "delivery_preferences": {"channels": ["email", "sms"], "alert_threshold": "HIGH"},
            "pause_until": "2026-10-01"  // pause alerts until this date, null to resume
        }
    }
    """
    try:
        body = _parse_body(event)
    except json.JSONDecodeError as e:
        return _response(400, {"error": "invalid_json", "message": f"Invalid JSON in request body: {e}"})
    try:

        county_fips = body.get("county_fips")
        subscription_id = body.get("subscription_id")
        updates = body.get("updates", {})

        if not county_fips or not subscription_id:
            return _response(400, {"error": "Missing county_fips and/or subscription_id"})

        if not updates:
            return _response(400, {"error": "No updates provided"})

        # Validate therapeutic_categories separately for specific error format
        if "therapeutic_categories" in updates:
            categories = updates["therapeutic_categories"]
            if not isinstance(categories, list):
                return _response(400, {"error": "therapeutic_categories must be an array"})
            config = _load_therapeutic_category_config()
            valid_keys = {cat["category_key"] for cat in config.get("categories", [])}
            for cat_key in categories:
                if cat_key not in valid_keys:
                    return _response(400, {"error": f"Invalid therapeutic category: {cat_key}"})

        # Validate the updates
        _validate_updates(updates)

        # Fetch existing subscription
        response = table.get_item(
            Key={"county_fips": county_fips, "subscription_id": subscription_id}
        )
        item = response.get("Item")

        if not item:
            return _response(404, {"error": "Subscription not found"})

        if item.get("status") == "inactive":
            return _response(410, {"error": "Subscription is inactive. Re-subscribe to make changes."})

        # Build update expression
        update_expr_parts = ["updated_at = :now"]
        expr_values = {":now": datetime.utcnow().isoformat()}
        expr_names = {}

        for field, value in updates.items():
            if field not in UPDATABLE_FIELDS:
                continue

            # Handle pause/resume
            if field == "pause_until":
                if value:
                    update_expr_parts.append("#s = :paused_status")
                    update_expr_parts.append("pause_until = :pause_until")
                    expr_values[":paused_status"] = "paused"
                    expr_values[":pause_until"] = value
                    expr_names["#s"] = "status"
                else:
                    # Resume: clear pause
                    update_expr_parts.append("#s = :active_status")
                    update_expr_parts.append("pause_until = :null_val")
                    expr_values[":active_status"] = "active"
                    expr_values[":null_val"] = None
                    expr_names["#s"] = "status"
            elif field == "therapeutic_categories":
                # Remove duplicates before saving
                deduped = list(dict.fromkeys(value))
                safe_key = "#f_therapeutic_categories"
                val_key = ":v_therapeutic_categories"
                update_expr_parts.append(f"{safe_key} = {val_key}")
                expr_names[safe_key] = field
                expr_values[val_key] = deduped
            else:
                safe_key = f"#f_{field}"
                val_key = f":v_{field}"
                update_expr_parts.append(f"{safe_key} = {val_key}")
                expr_names[safe_key] = field
                expr_values[val_key] = value

        update_expression = "SET " + ", ".join(update_expr_parts)

        # Execute update
        update_kwargs = {
            "Key": {"county_fips": county_fips, "subscription_id": subscription_id},
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": {k: v for k, v in expr_values.items() if v is not None},
            "ReturnValues": "ALL_NEW",
        }
        if expr_names:
            update_kwargs["ExpressionAttributeNames"] = expr_names

        result = table.update_item(**update_kwargs)
        updated_item = result.get("Attributes", {})

        logger.info(f"Updated subscription {subscription_id}: {list(updates.keys())}")

        return _response(200, {
            "message": "Preferences updated successfully.",
            "subscription_id": subscription_id,
            "updated_fields": list(updates.keys()),
            "current_status": updated_item.get("status", "unknown"),
            "pause_until": updated_item.get("pause_until"),
        })

    except ValidationError as e:
        return _response(400, {"error": "validation_error", "message": str(e)})
    except Exception as e:
        logger.error(f"Update failed: {e}", exc_info=True)
        return _response(500, {"error": "internal_error", "message": "Update failed."})


def _validate_updates(updates: dict) -> None:
    """Validate update fields."""
    # Check for invalid field names
    invalid = set(updates.keys()) - UPDATABLE_FIELDS
    if invalid:
        raise ValidationError(f"Cannot update fields: {invalid}. Allowed: {UPDATABLE_FIELDS}")

    # Validate email format if being changed
    if "contact_email" in updates:
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", updates["contact_email"]):
            raise ValidationError(f"Invalid email format: {updates['contact_email']}")

    # Validate phone format if being changed
    if "contact_phone" in updates and updates["contact_phone"]:
        if not re.match(r"^\+\d{10,15}$", updates["contact_phone"]):
            raise ValidationError("Invalid phone format: must be E.164")

    # Validate diseases if being changed
    if "diseases" in updates:
        active = list_active_diseases()
        for d in updates["diseases"]:
            if d.lower() not in [a.lower() for a in active]:
                raise ValidationError(f"Disease '{d}' not configured. Active: {active}")

    # Validate pause_until date format
    if "pause_until" in updates and updates["pause_until"]:
        try:
            datetime.fromisoformat(updates["pause_until"])
        except ValueError:
            raise ValidationError("pause_until must be ISO date format (YYYY-MM-DD)")

    # Validate delivery_preferences structure
    if "delivery_preferences" in updates:
        prefs = updates["delivery_preferences"]
        if not isinstance(prefs, dict):
            raise ValidationError("delivery_preferences must be an object")
        valid_channels = {"email", "sms"}
        channels = prefs.get("channels", [])
        invalid_channels = set(channels) - valid_channels
        if invalid_channels:
            raise ValidationError(f"Invalid channels: {invalid_channels}. Valid: {valid_channels}")


def _parse_body(event: dict) -> dict:
    body = event.get("body", "")
    if isinstance(body, str):
        return json.loads(body) if body else {}
    return body or {}


def _load_therapeutic_category_config() -> dict:
    """Load therapeutic category configuration from S3."""
    try:
        config_key = f"{CONFIG_PREFIX}shortage_monitoring/therapeutic_categories.json"
        response = s3_client.get_object(Bucket=CONFIG_BUCKET, Key=config_key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to load therapeutic category config: {e}")
        # Return default config with basic categories
        return {
            "categories": [
                {"category_key": "antivirals"},
                {"category_key": "antibiotics"},
            ]
        }


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


class ValidationError(Exception):
    pass
