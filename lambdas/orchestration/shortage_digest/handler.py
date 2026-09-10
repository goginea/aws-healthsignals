"""Weekly Drug Shortage Digest generator.

Triggered by an EventBridge weekly schedule (Monday 08:00 UTC, after the
weekly openFDA fetch + change detection have run). Compiles a single digest
covering the current reporting week:

    - Top 10 most significant NEW / WORSENING shortages (ranked)
    - Top 5 most recently RESOLVED shortages

and starts ONE Step Functions execution (alert_type="shortage_digest") that
generates a digest brief with Bedrock and dispatches it to all shortage
subscribers. Unlike the per-product alert path, the digest is a single
summary rather than one alert per product.

This Lambda is part of the Drug Shortage Intelligence plugin module and has no
coupling to the core pipeline coordinator code.

Environment Variables:
    SHORTAGE_STATE_TABLE: DynamoDB table for shortage state
    STATE_MACHINE_ARN: Step Functions ARN for shortage alert generation
    LOG_LEVEL: Logging level
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

# Add shared module to path (Lambda Layer handles this in prod)
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")

SHORTAGE_STATE_TABLE = os.environ.get(
    "SHORTAGE_STATE_TABLE", "healthsignals-drug-shortage-state"
)
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

state_table = dynamodb.Table(SHORTAGE_STATE_TABLE)

# Ranking knobs
TOP_ACTIVE_LIMIT = 10
TOP_RESOLVED_LIMIT = 5

# Higher = more severe; used to rank active shortages within a status tier.
_SUPPLY_SEVERITY = {
    "DISCONTINUED": 3,
    "CURRENTLY_IN_SHORTAGE": 2,
    "IN_SHORTAGE": 2,
    "LIMITED_AVAILABILITY": 1,
    "UNKNOWN": 0,
    "AVAILABLE": 0,
}
# WORSENING ranks above NEW.
_STATUS_RANK = {"WORSENING": 1, "NEW": 0}


def lambda_handler(event: dict, context: Any) -> dict:
    """Compile and dispatch the weekly shortage digest.

    Accepts an optional {"week_timestamp": "YYYY-Www"} override; defaults to the
    current ISO week.
    """
    week_timestamp = event.get("week_timestamp") or _get_current_iso_week()
    logger.info(json.dumps({
        "event_type": "digest_start",
        "week_timestamp": week_timestamp,
    }))

    records = _load_week_records(week_timestamp)

    active = [r for r in records if r.get("shortage_status") in ("NEW", "WORSENING")]
    resolved = [r for r in records if r.get("shortage_status") == "RESOLVED"]

    top_active = sorted(active, key=_active_sort_key, reverse=True)[:TOP_ACTIVE_LIMIT]
    recently_resolved = sorted(
        resolved, key=lambda r: r.get("created_at", ""), reverse=True
    )[:TOP_RESOLVED_LIMIT]

    if not top_active and not recently_resolved:
        logger.info(json.dumps({
            "event_type": "digest_skipped_no_activity",
            "week_timestamp": week_timestamp,
        }))
        return {
            "statusCode": 200,
            "week_timestamp": week_timestamp,
            "dispatched": False,
            "reason": "no_shortage_activity",
        }

    payload = {
        "alert_type": "shortage_digest",
        "week_timestamp": week_timestamp,
        "top_active": [_slim(r) for r in top_active],
        "recently_resolved": [_slim(r) for r in recently_resolved],
        # therapeutic_category is required by the dispatcher's generic shortage
        # path; the digest dispatch ignores it, but keep a stable label.
        "therapeutic_category": "weekly-digest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    execution_arn = _start_digest(payload, week_timestamp)

    return {
        "statusCode": 200,
        "week_timestamp": week_timestamp,
        "dispatched": bool(execution_arn),
        "execution_arn": execution_arn,
        "top_active_count": len(top_active),
        "recently_resolved_count": len(recently_resolved),
    }


def _load_week_records(week_timestamp: str) -> list:
    """Scan the shortage-state table for all records in the given week."""
    records = []
    try:
        response = state_table.scan(
            FilterExpression="week_timestamp = :w",
            ExpressionAttributeValues={":w": week_timestamp},
        )
        records.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = state_table.scan(
                FilterExpression="week_timestamp = :w",
                ExpressionAttributeValues={":w": week_timestamp},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            records.extend(response.get("Items", []))
    except Exception as e:
        logger.error(f"Failed to scan shortage-state for week {week_timestamp}: {e}")
    return records


def _active_sort_key(record: dict) -> tuple:
    """Rank active shortages: WORSENING above NEW, then by supply-status severity."""
    status_rank = _STATUS_RANK.get(record.get("shortage_status"), 0)
    supply_rank = _SUPPLY_SEVERITY.get(record.get("supply_status", "UNKNOWN"), 0)
    return (status_rank, supply_rank)


def _slim(record: dict) -> dict:
    """Compact record for the digest brief prompt."""
    return {
        "product_name": record.get("product_name", ""),
        "therapeutic_category": record.get("therapeutic_category", "uncategorized"),
        "supply_status": record.get("supply_status", "UNKNOWN"),
        "shortage_status": record.get("shortage_status", ""),
        "reason_for_shortage": record.get("reason_for_shortage", "Unknown"),
    }


def _start_digest(payload: dict, week_timestamp: str) -> str | None:
    """Start one Step Functions execution for the weekly digest."""
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not configured — skipping digest SFN")
        return None
    exec_name = f"shortage-digest-{week_timestamp}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    exec_name = exec_name[:80].replace("/", "-").replace(" ", "-")
    try:
        response = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(payload),
        )
        logger.info(json.dumps({
            "event_type": "digest_sfn_started",
            "execution_arn": response["executionArn"],
            "week_timestamp": week_timestamp,
        }))
        return response["executionArn"]
    except Exception as e:
        logger.error(f"Failed to start digest SFN for {week_timestamp}: {e}")
        return None


def _get_current_iso_week() -> str:
    """Get current ISO week as YYYY-Www format (matches change detector)."""
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"
