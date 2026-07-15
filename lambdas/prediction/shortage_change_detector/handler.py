"""Shortage Change Detector — Compares current openFDA shortage data against DynamoDB
historical state to classify changes as NEW, WORSENING, RESOLVED, or UNCHANGED.

Triggered by: S3 PutObject event notification when new openFDA data lands,
              or direct invocation with s3_key and week_timestamp.

Logic:
    1. Load current shortage data from S3
    2. Load therapeutic categories config for category filtering
    3. Query DynamoDB shortage-state table for previous week records
    4. Classify each record: NEW, WORSENING, RESOLVED, UNCHANGED
    5. Filter by therapeutic categories (exclude "uncategorized")
    6. Circuit breaker: if NEW+WORSENING > 20, emit alarm and return
    7. Write/update shortage-state table with current state
    8. Check idempotency against shortage-alerts table
    9. Write PENDING alert records for NEW/WORSENING shortages
    10. Invoke Step Functions for alertable changes

Environment Variables:
    DATA_BUCKET: S3 bucket with shortage data
    SHORTAGE_STATE_TABLE: DynamoDB table for historical shortage state
    SHORTAGE_ALERTS_TABLE: DynamoDB table for alert idempotency tracking
    STATE_MACHINE_ARN: Step Functions ARN for alert generation
    CONFIG_BUCKET: S3 bucket containing config files
    CONFIG_PREFIX: S3 key prefix for config files
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# Add shared module to path (for local development — in Lambda, the Layer handles this)
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import _load_config, get_system_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# AWS clients
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
sfn = boto3.client("stepfunctions")

# Configuration from environment
system = get_system_config()
DATA_BUCKET = os.environ.get(
    "DATA_BUCKET", system["infrastructure"]["data_bucket_name_pattern"]
)
SHORTAGE_STATE_TABLE = os.environ.get(
    "SHORTAGE_STATE_TABLE", "healthsignals-drug-shortage-state"
)
SHORTAGE_ALERTS_TABLE = os.environ.get(
    "SHORTAGE_ALERTS_TABLE", "healthsignals-shortage-alerts"
)
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

# DynamoDB table resources
state_table = dynamodb.Table(SHORTAGE_STATE_TABLE)
alerts_table = dynamodb.Table(SHORTAGE_ALERTS_TABLE)

# Constants
METRIC_NAMESPACE = "HealthSignals/DrugShortages"
FUNCTION_NAME = "shortage_change_detector"
CIRCUIT_BREAKER_THRESHOLD = 20


def lambda_handler(event: dict, context: Any) -> dict:
    """Detect shortage changes by comparing current data against DynamoDB state.

    Accepts two event formats:

    1. S3 Event Notification (triggered directly by S3 PutObject):
    {
        "Records": [{
            "s3": {
                "bucket": {"name": "healthsignals-data-..."},
                "object": {"key": "raw/openfda-shortages/2024/W03/shortages_20240115.json"}
            }
        }]
    }

    2. Direct invocation (for testing or manual trigger):
    {
        "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
        "week_timestamp": "2024-W03"
    }

    Returns:
    {
        "changes_detected": {"NEW": N, "WORSENING": N, "RESOLVED": N, "UNCHANGED": N},
        "alerts_triggered": N,
        "circuit_breaker_activated": false
    }
    """
    log_structured("info", "handler_start", {"event": event})

    # Parse event — support both S3 notification and direct invocation
    s3_key, week_timestamp = _parse_event(event)

    log_structured("info", "event_parsed", {
        "s3_key": s3_key,
        "week_timestamp": week_timestamp,
    })

    # 1. Load current shortage data from S3
    current_data = load_shortage_data_from_s3(s3_key)
    log_structured("info", "current_data_loaded", {
        "s3_key": s3_key,
        "record_count": len(current_data),
    })

    # 2. Load therapeutic categories config for filtering
    therapeutic_config = load_therapeutic_config()
    monitored_categories = get_monitored_categories(therapeutic_config)
    log_structured("info", "therapeutic_config_loaded", {
        "monitored_categories": monitored_categories,
    })

    # 3. Query DynamoDB for previous week records
    previous_week = compute_previous_week(week_timestamp)
    previous_state = load_previous_state(previous_week)
    log_structured("info", "previous_state_loaded", {
        "previous_week": previous_week,
        "record_count": len(previous_state),
    })

    # 4. Classify changes
    changes = classify_changes(current_data, previous_state)
    log_structured("info", "changes_classified", {
        "NEW": len(changes["NEW"]),
        "WORSENING": len(changes["WORSENING"]),
        "RESOLVED": len(changes["RESOLVED"]),
        "UNCHANGED": len(changes["UNCHANGED"]),
    })

    # 5. Filter by therapeutic categories (exclude uncategorized)
    filtered_changes = filter_by_therapeutic_categories(changes, monitored_categories)
    log_structured("info", "changes_filtered", {
        "NEW": len(filtered_changes["NEW"]),
        "WORSENING": len(filtered_changes["WORSENING"]),
        "RESOLVED": len(filtered_changes["RESOLVED"]),
        "UNCHANGED": len(filtered_changes["UNCHANGED"]),
    })

    # 6. Circuit breaker check
    alertable_count = len(filtered_changes["NEW"]) + len(filtered_changes["WORSENING"])
    if alertable_count > CIRCUIT_BREAKER_THRESHOLD:
        log_structured("warning", "circuit_breaker_activated", {
            "new_count": len(filtered_changes["NEW"]),
            "worsening_count": len(filtered_changes["WORSENING"]),
            "threshold": CIRCUIT_BREAKER_THRESHOLD,
        })
        emit_circuit_breaker_alarm(alertable_count)
        emit_metrics(
            changes_detected=sum(len(v) for v in filtered_changes.values()),
            alerts_generated=0,
            circuit_breaker=True,
        )
        return {
            "changes_detected": {
                "NEW": len(filtered_changes["NEW"]),
                "WORSENING": len(filtered_changes["WORSENING"]),
                "RESOLVED": len(filtered_changes["RESOLVED"]),
                "UNCHANGED": len(filtered_changes["UNCHANGED"]),
            },
            "alerts_triggered": 0,
            "circuit_breaker_activated": True,
        }

    # 7. Write/update DynamoDB shortage-state table for ALL current records
    write_current_state(current_data, previous_state, week_timestamp)

    # 8-11. Process alertable changes (NEW + WORSENING)
    alerts_triggered = 0
    alertable_records = filtered_changes["NEW"] + filtered_changes["WORSENING"]

    for record in alertable_records:
        product_id = record["product_id"]

        # 8. Check idempotency
        if is_alert_already_sent(product_id, week_timestamp):
            log_structured("info", "alert_skipped_idempotency", {
                "product_id": product_id,
                "week_timestamp": week_timestamp,
            })
            continue

        # 9. Write PENDING alert record
        write_pending_alert(record, week_timestamp)

        # 10. Invoke Step Functions
        execution_arn = invoke_step_functions(record, week_timestamp)
        if execution_arn:
            alerts_triggered += 1
            update_alert_execution_arn(product_id, week_timestamp, execution_arn)

    # Emit CloudWatch metrics
    emit_metrics(
        changes_detected=sum(len(v) for v in filtered_changes.values()),
        alerts_generated=alerts_triggered,
        circuit_breaker=False,
    )

    log_structured("info", "handler_complete", {
        "changes_detected": {
            "NEW": len(filtered_changes["NEW"]),
            "WORSENING": len(filtered_changes["WORSENING"]),
            "RESOLVED": len(filtered_changes["RESOLVED"]),
            "UNCHANGED": len(filtered_changes["UNCHANGED"]),
        },
        "alerts_triggered": alerts_triggered,
    })

    return {
        "changes_detected": {
            "NEW": len(filtered_changes["NEW"]),
            "WORSENING": len(filtered_changes["WORSENING"]),
            "RESOLVED": len(filtered_changes["RESOLVED"]),
            "UNCHANGED": len(filtered_changes["UNCHANGED"]),
        },
        "alerts_triggered": alerts_triggered,
        "circuit_breaker_activated": False,
    }


# === Event Parsing ===


def _parse_event(event: dict) -> tuple[str, str]:
    """Parse the incoming event to extract s3_key and week_timestamp.

    Supports S3 event notification format and direct invocation format.

    Returns:
        Tuple of (s3_key, week_timestamp).

    Raises:
        ValueError if the event cannot be parsed.
    """
    if "Records" in event:
        # S3 Event Notification format
        record = event["Records"][0]
        s3_key = record["s3"]["object"]["key"]
        week_timestamp = _extract_week_from_s3_key(s3_key)
        return s3_key, week_timestamp
    elif "s3_key" in event:
        # Direct invocation format
        s3_key = event["s3_key"]
        week_timestamp = event.get("week_timestamp") or _extract_week_from_s3_key(s3_key)
        return s3_key, week_timestamp
    else:
        raise ValueError(f"Unrecognized event format: {json.dumps(event)[:200]}")


def _extract_week_from_s3_key(s3_key: str) -> str:
    """Extract week_timestamp from openFDA shortage S3 key.

    Args:
        s3_key: e.g., "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json"

    Returns:
        Week timestamp in ISO format, e.g., "2024-W03"
    """
    parts = s3_key.split("/")
    # Expected: ["raw", "openfda-shortages", "2024", "W03", "shortages_20240115_060512.json"]
    if len(parts) >= 4:
        year = parts[2]
        week_part = parts[3]  # e.g., "W03"
        week_num = week_part.replace("W", "").zfill(2)
        return f"{year}-W{week_num}"

    # Fallback to current ISO week if parsing fails
    log_structured("warning", "week_extraction_fallback", {"s3_key": s3_key})
    now = datetime.now(timezone.utc)
    iso_cal = now.isocalendar()
    return f"{iso_cal[0]}-W{iso_cal[1]:02d}"


# === Data Loading ===


def load_shortage_data_from_s3(s3_key: str) -> list[dict]:
    """Load normalized shortage records from S3."""
    response = s3.get_object(Bucket=DATA_BUCKET, Key=s3_key)
    content = response["Body"].read().decode("utf-8")
    return json.loads(content)


def load_therapeutic_config() -> dict:
    """Load therapeutic categories configuration from S3 or local filesystem."""
    return _load_config("shortage_monitoring/therapeutic_categories.json")


def get_monitored_categories(config: dict) -> set[str]:
    """Extract set of monitored category keys from therapeutic config."""
    categories = config.get("categories", [])
    return {cat["category_key"] for cat in categories}


# === Previous State Management ===


def compute_previous_week(week_timestamp: str) -> str:
    """Compute the previous ISO week timestamp.

    Args:
        week_timestamp: ISO week string e.g. "2024-W03"

    Returns:
        Previous week string e.g. "2024-W02", handling year boundaries.
    """
    parts = week_timestamp.split("-W")
    year = int(parts[0])
    week = int(parts[1])

    if week > 1:
        return f"{year}-W{week - 1:02d}"
    else:
        # Roll back to last week of previous year (ISO 8601: either 52 or 53)
        prev_year = year - 1
        # Determine number of ISO weeks in previous year
        # A year has 53 weeks if Jan 1 is Thursday, or Dec 31 is Thursday
        from datetime import date
        dec_31 = date(prev_year, 12, 31)
        last_week = dec_31.isocalendar()[1]
        # isocalendar can return week 1 of next year for Dec 31
        if last_week == 1:
            last_week = date(prev_year, 12, 28).isocalendar()[1]
        return f"{prev_year}-W{last_week:02d}"


def load_previous_state(previous_week: str) -> dict[str, dict]:
    """Query DynamoDB for all shortage state records from the previous week.

    Returns:
        dict mapping product_id → record dict for the previous week.
    """
    previous_state = {}

    # Scan with filter for the previous week_timestamp
    # For production scale (~1600 records), a scan with filter is acceptable
    # since we need all records for that week
    try:
        response = state_table.scan(
            FilterExpression=Key("week_timestamp").eq(previous_week)
        )
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = state_table.scan(
                FilterExpression=Key("week_timestamp").eq(previous_week),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        for item in items:
            previous_state[item["product_id"]] = item

    except Exception as e:
        log_structured("error", "dynamodb_query_failed", {
            "table": SHORTAGE_STATE_TABLE,
            "previous_week": previous_week,
            "error": str(e),
        })
        raise

    return previous_state


# === Change Classification ===


def classify_changes(
    current_data: list[dict], previous_state: dict[str, dict]
) -> dict[str, list[dict]]:
    """Classify each shortage record by comparing current vs previous state.

    Classification rules:
        NEW: product_id in current data but NOT in previous DynamoDB state
        WORSENING: supply_status changed from AVAILABLE to DISCONTINUED,
                   or reason_for_shortage changed
        RESOLVED: product_id in previous DynamoDB state but NOT in current data
        UNCHANGED: all fields match previous week

    Args:
        current_data: List of normalized shortage records from S3.
        previous_state: Dict mapping product_id → previous week record from DynamoDB.

    Returns:
        Dict with keys NEW, WORSENING, RESOLVED, UNCHANGED, each containing a list
        of records annotated with shortage_status and previous_supply_status.
    """
    changes: dict[str, list[dict]] = {
        "NEW": [],
        "WORSENING": [],
        "RESOLVED": [],
        "UNCHANGED": [],
    }

    current_product_ids = set()

    for record in current_data:
        product_id = record["product_id"]
        current_product_ids.add(product_id)

        if product_id not in previous_state:
            # NEW: not in previous DynamoDB state
            annotated = {**record, "shortage_status": "NEW", "previous_supply_status": None}
            changes["NEW"].append(annotated)
        else:
            prev = previous_state[product_id]
            prev_status = prev.get("supply_status", "UNKNOWN")
            curr_status = record.get("supply_status", "UNKNOWN")
            prev_reason = prev.get("reason_for_shortage", "Unknown")
            curr_reason = record.get("reason_for_shortage", "Unknown")

            if is_worsening(prev_status, curr_status, prev_reason, curr_reason):
                annotated = {
                    **record,
                    "shortage_status": "WORSENING",
                    "previous_supply_status": prev_status,
                }
                changes["WORSENING"].append(annotated)
            else:
                annotated = {
                    **record,
                    "shortage_status": "UNCHANGED",
                    "previous_supply_status": prev_status,
                }
                changes["UNCHANGED"].append(annotated)

    # RESOLVED: in previous state but not in current data
    for product_id, prev_record in previous_state.items():
        if product_id not in current_product_ids:
            annotated = {
                **prev_record,
                "shortage_status": "RESOLVED",
                "previous_supply_status": prev_record.get("supply_status"),
            }
            changes["RESOLVED"].append(annotated)

    return changes


def is_worsening(
    prev_status: str, curr_status: str, prev_reason: str, curr_reason: str
) -> bool:
    """Determine if a shortage has worsened.

    Worsening conditions:
        - supply_status changed from AVAILABLE to DISCONTINUED
        - reason_for_shortage changed (indicates new/different cause)
    """
    if prev_status == "AVAILABLE" and curr_status == "DISCONTINUED":
        return True
    if prev_reason != curr_reason:
        return True
    return False


# === Therapeutic Category Filtering ===


def filter_by_therapeutic_categories(
    changes: dict[str, list[dict]], monitored_categories: set[str]
) -> dict[str, list[dict]]:
    """Filter changes to only include records in monitored therapeutic categories.

    Excludes records where therapeutic_category is "uncategorized" or not in the
    configured category list.
    """
    filtered: dict[str, list[dict]] = {
        "NEW": [],
        "WORSENING": [],
        "RESOLVED": [],
        "UNCHANGED": [],
    }

    for status, records in changes.items():
        for record in records:
            category = record.get("therapeutic_category", "uncategorized")
            if category != "uncategorized" and category in monitored_categories:
                filtered[status].append(record)

    return filtered


# === DynamoDB State Writes ===


def write_current_state(
    current_data: list[dict], previous_state: dict[str, dict], week_timestamp: str
) -> None:
    """Write/update DynamoDB shortage-state table with current week state.

    For each product in current data, writes a record with current state
    and previous_supply_status for historical tracking.
    """
    now = datetime.now(timezone.utc).isoformat()
    # TTL: 52 weeks from now
    ttl_value = int(datetime.now(timezone.utc).timestamp()) + (52 * 7 * 24 * 3600)

    with state_table.batch_writer() as batch:
        for record in current_data:
            product_id = record["product_id"]
            prev = previous_state.get(product_id, {})
            prev_status = prev.get("supply_status")

            # Determine shortage_status for this record
            if product_id not in previous_state:
                shortage_status = "NEW"
            elif is_worsening(
                prev.get("supply_status", "UNKNOWN"),
                record.get("supply_status", "UNKNOWN"),
                prev.get("reason_for_shortage", "Unknown"),
                record.get("reason_for_shortage", "Unknown"),
            ):
                shortage_status = "WORSENING"
            else:
                shortage_status = "UNCHANGED"

            item = {
                "product_id": product_id,
                "week_timestamp": week_timestamp,
                "product_name": record.get("product_name", ""),
                "therapeutic_category": record.get("therapeutic_category", "uncategorized"),
                "supply_status": record.get("supply_status", "UNKNOWN"),
                "reason_for_shortage": record.get("reason_for_shortage", "Unknown"),
                "estimated_resolution_date": record.get("estimated_resolution_date"),
                "shortage_status": shortage_status,
                "previous_supply_status": prev_status,
                "created_at": now,
                "ttl": ttl_value,
            }
            # Remove None values (DynamoDB doesn't accept None for non-key attrs)
            item = {k: v for k, v in item.items() if v is not None}
            batch.put_item(Item=item)

    log_structured("info", "state_table_updated", {
        "records_written": len(current_data),
        "week_timestamp": week_timestamp,
    })


# === Idempotency ===


def is_alert_already_sent(product_id: str, week_timestamp: str) -> bool:
    """Check if an alert was already generated for this product_id and week.

    Returns True if a record exists with alert_generated=SENT.
    Returns False if no record exists, or record exists with FAILED (allows retry).
    """
    try:
        response = alerts_table.get_item(
            Key={"product_id": product_id, "week_timestamp": week_timestamp}
        )
        item = response.get("Item")
        if not item:
            return False
        # Skip if already SENT
        if item.get("alert_generated") == "SENT":
            return True
        # Allow retry if FAILED and retry_count < 3
        if item.get("alert_generated") == "FAILED":
            retry_count = item.get("retry_count", 0)
            if retry_count < 3:
                return False
            else:
                return True  # Exhausted retries
        # PENDING — might be in progress, skip to avoid duplicate
        if item.get("alert_generated") == "PENDING":
            return True
        return False
    except Exception as e:
        log_structured("error", "idempotency_check_failed", {
            "product_id": product_id,
            "week_timestamp": week_timestamp,
            "error": str(e),
        })
        # Fail open — allow alert generation if check fails
        return False


def write_pending_alert(record: dict, week_timestamp: str) -> None:
    """Write a PENDING alert record to the shortage-alerts table."""
    now = datetime.now(timezone.utc).isoformat()

    item = {
        "product_id": record["product_id"],
        "week_timestamp": week_timestamp,
        "therapeutic_category": record.get("therapeutic_category", "uncategorized"),
        "shortage_status": record.get("shortage_status", "NEW"),
        "detection_timestamp": now,
        "alert_generated": "PENDING",
        "retry_count": 0,
        "created_at": now,
    }
    try:
        alerts_table.put_item(Item=item)
        log_structured("info", "alert_record_created", {
            "product_id": record["product_id"],
            "status": "PENDING",
        })
    except Exception as e:
        log_structured("error", "alert_record_write_failed", {
            "product_id": record["product_id"],
            "error": str(e),
        })
        raise


def update_alert_execution_arn(
    product_id: str, week_timestamp: str, execution_arn: str
) -> None:
    """Update alert record with Step Functions execution ARN."""
    try:
        alerts_table.update_item(
            Key={"product_id": product_id, "week_timestamp": week_timestamp},
            UpdateExpression="SET step_function_execution_arn = :arn",
            ExpressionAttributeValues={":arn": execution_arn},
        )
    except Exception as e:
        log_structured("warning", "alert_arn_update_failed", {
            "product_id": product_id,
            "error": str(e),
        })


# === Step Functions Invocation ===


def invoke_step_functions(record: dict, week_timestamp: str) -> str | None:
    """Invoke Step Functions state machine for alert generation.

    Args:
        record: Annotated shortage record with shortage_status.
        week_timestamp: Current ISO week.

    Returns:
        Execution ARN if successful, None otherwise.
    """
    if not STATE_MACHINE_ARN:
        log_structured("warning", "step_functions_skipped", {
            "reason": "STATE_MACHINE_ARN not configured",
        })
        return None

    payload = {
        "alert_type": "shortage",
        "product_id": record["product_id"],
        "product_name": record.get("product_name", ""),
        "therapeutic_category": record.get("therapeutic_category", ""),
        "supply_status": record.get("supply_status", "UNKNOWN"),
        "reason_for_shortage": record.get("reason_for_shortage", "Unknown"),
        "estimated_resolution_date": record.get("estimated_resolution_date"),
        "shortage_status": record.get("shortage_status"),
        "previous_supply_status": record.get("previous_supply_status"),
        "week_timestamp": week_timestamp,
        "detection_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Remove None values from payload
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        execution_name = (
            f"shortage-{record['product_id']}-{week_timestamp}-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )
        # Step Functions execution names must be <= 80 chars and alphanumeric + hyphens
        execution_name = execution_name[:80].replace("/", "-").replace(" ", "-")

        response = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps(payload),
        )
        execution_arn = response["executionArn"]
        log_structured("info", "step_functions_invoked", {
            "product_id": record["product_id"],
            "execution_arn": execution_arn,
            "shortage_status": record.get("shortage_status"),
        })
        return execution_arn

    except Exception as e:
        log_structured("error", "step_functions_invocation_failed", {
            "product_id": record["product_id"],
            "error": str(e),
        })
        return None


# === CloudWatch Metrics ===


def emit_metrics(changes_detected: int, alerts_generated: int, circuit_breaker: bool) -> None:
    """Emit CloudWatch metrics for shortage change detection."""
    try:
        metric_data = [
            {
                "MetricName": "shortage_changes_detected_count",
                "Value": changes_detected,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "FunctionName", "Value": FUNCTION_NAME},
                ],
            },
            {
                "MetricName": "shortage_alerts_generated_count",
                "Value": alerts_generated,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "FunctionName", "Value": FUNCTION_NAME},
                ],
            },
        ]

        if circuit_breaker:
            metric_data.append({
                "MetricName": "shortage_circuit_breaker_activations",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "FunctionName", "Value": FUNCTION_NAME},
                ],
            })

        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
    except Exception as e:
        log_structured("error", "metrics_emission_failed", {"error": str(e)})


def emit_circuit_breaker_alarm(count: int) -> None:
    """Emit CloudWatch alarm metric when circuit breaker activates."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "shortage_circuit_breaker_activations",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "FunctionName", "Value": FUNCTION_NAME},
                    ],
                },
            ],
        )
        log_structured("warning", "circuit_breaker_alarm_emitted", {
            "alertable_count": count,
            "threshold": CIRCUIT_BREAKER_THRESHOLD,
        })
    except Exception as e:
        log_structured("error", "circuit_breaker_alarm_failed", {"error": str(e)})


# === Structured Logging ===


def log_structured(level: str, event_type: str, metadata: dict = None) -> None:
    """Emit structured JSON log entry following HealthSignals logging standards."""
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "level": level.upper(),
        "function_name": FUNCTION_NAME,
        "event_type": event_type,
    }
    if metadata:
        log_entry["metadata"] = metadata

    message = json.dumps(log_entry, default=str)

    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.info(message)
