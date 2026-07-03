"""Feedback Collector — Captures accuracy feedback from health officers.

After each alert, health officers can submit feedback on:
1. Did the predicted outbreak actually materialize? (Y/N)
2. Was the timing roughly accurate? (early/on-time/late)
3. Was the severity estimate useful? (over/about-right/under)
4. Would you want this alert again? (Y/N)

This feedback feeds back into the calibration table to improve future predictions.
It also provides the validation data needed for the "prospective validation" milestone.

When ≥ RECALIBRATION_THRESHOLD responses accumulate for a given alert_id,
the feedback_recalibrator Lambda is invoked to update calibration data.
"""
import json
import os
import logging
from datetime import datetime
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEEDBACK_TABLE = os.environ.get("FEEDBACK_TABLE", "healthsignals-feedback")
RECALIBRATOR_FUNCTION_NAME = os.environ.get(
    "RECALIBRATOR_FUNCTION_NAME", "healthsignals-feedback-recalibrator"
)
RECALIBRATION_THRESHOLD = int(os.environ.get("RECALIBRATION_THRESHOLD", "3"))

dynamodb = boto3.resource("dynamodb")
feedback_table = dynamodb.Table(FEEDBACK_TABLE)
lambda_client = boto3.client("lambda")


def lambda_handler(event: dict, context: Any) -> dict:
    """Accept and store feedback from health officers.

    Input event (from API Gateway):
    {
        "httpMethod": "POST",
        "body": "{
            \"alert_id\": \"flu_48143_202645\",
            \"county_fips\": \"48143\",
            \"outbreak_occurred\": true,
            \"timing_accuracy\": \"on-time\",
            \"severity_accuracy\": \"about-right\",
            \"would_use_again\": true,
            \"free_text\": \"Helped us pre-order flu tests 3 weeks early.\"
        }"
    }

    Returns:
        API Gateway response with 200/400/500.
    """
    try:
        # Parse request
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        else:
            body = event.get("body", event)

        # Validate required fields
        required_fields = ["alert_id", "county_fips"]
        for field in required_fields:
            if field not in body:
                return api_response(400, {"error": f"Missing required field: {field}"})

        # Store feedback
        feedback_item = {
            "alert_id": body["alert_id"],
            "county_fips": body["county_fips"],
            "submitted_at": datetime.utcnow().isoformat(),
            "outbreak_occurred": body.get("outbreak_occurred"),
            "timing_accuracy": body.get("timing_accuracy"),  # early|on-time|late|not-applicable
            "severity_accuracy": body.get("severity_accuracy"),  # over|about-right|under
            "would_use_again": body.get("would_use_again"),
            "free_text": body.get("free_text", ""),
        }

        feedback_table.put_item(Item=feedback_item)
        logger.info(f"Feedback stored for alert {body['alert_id']} from {body['county_fips']}")

        # Check if enough feedback has accumulated to trigger recalibration
        recalibration_triggered = check_and_trigger_recalibration(body["alert_id"])

        response_body = {"message": "Feedback recorded. Thank you!"}
        if recalibration_triggered:
            response_body["recalibration"] = "triggered"

        return api_response(200, response_body)

    except json.JSONDecodeError:
        return api_response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        logger.error(f"Feedback processing failed: {e}")
        return api_response(500, {"error": "Internal error processing feedback"})


def check_and_trigger_recalibration(alert_id: str) -> bool:
    """Check if enough feedback has accumulated for this alert to trigger recalibration.

    When >= RECALIBRATION_THRESHOLD responses exist for the same alert_id,
    asynchronously invoke the feedback_recalibrator Lambda.

    Returns True if recalibration was triggered, False otherwise.
    """
    try:
        # Count feedback records for this alert_id
        response = feedback_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("alert_id").eq(alert_id),
            Select="COUNT",
        )
        count = response.get("Count", 0)

        logger.info(f"Feedback count for {alert_id}: {count} (threshold: {RECALIBRATION_THRESHOLD})")

        if count >= RECALIBRATION_THRESHOLD:
            # Check if we already triggered recalibration for this alert
            # (avoid triggering multiple times as more feedback comes in)
            if count == RECALIBRATION_THRESHOLD:
                # Exactly at threshold = first trigger point
                invoke_recalibrator(alert_id)
                return True
            else:
                logger.info(f"Recalibration already triggered for {alert_id} (count > threshold)")
                return False

        return False

    except Exception as e:
        # Don't fail the feedback submission if recalibration check fails
        logger.warning(f"Recalibration check failed for {alert_id}: {e}")
        return False


def invoke_recalibrator(alert_id: str) -> None:
    """Asynchronously invoke the feedback_recalibrator Lambda.

    Uses InvocationType='Event' for fire-and-forget (non-blocking).
    The recalibrator will process all feedback for this alert and
    update the calibration table.
    """
    try:
        payload = {
            "alert_id": alert_id,
            "trigger_source": "feedback_threshold",
            "triggered_at": datetime.utcnow().isoformat(),
        }

        lambda_client.invoke(
            FunctionName=RECALIBRATOR_FUNCTION_NAME,
            InvocationType="Event",  # Async — don't wait for response
            Payload=json.dumps(payload),
        )

        logger.info(
            f"Recalibrator invoked for {alert_id} "
            f"(function: {RECALIBRATOR_FUNCTION_NAME})"
        )

    except Exception as e:
        # Log but don't fail — recalibration is best-effort
        logger.error(f"Failed to invoke recalibrator for {alert_id}: {e}")


def api_response(status_code: int, body: dict) -> dict:
    """Format Lambda response for API Gateway."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
