"""Pipeline Coordinator — End-to-end orchestration for HealthSignals.

This Lambda is the "brain" that chains the pipeline stages:
    Ingestion → Prediction → Generation → Delivery

Trigger: S3 Event Notification when new data lands under raw/delphi/
         (i.e., after the delphi_fetcher completes a weekly run)

Flow:
    1. Parse S3 event to determine which state/disease just got fresh data
    2. Load latest metro signals for that state from S3
    3. Invoke leader_detection Lambda synchronously
    4. If leader detected (new_alert=True):
       a. Invoke geographic_affinity to find affected counties
       b. Invoke timing_estimation for each county
       c. Start Step Functions execution for each county alert
       d. Emit EventBridge event for downstream modules (e.g., drug shortage enrichment)
    5. Log pipeline execution to DynamoDB (observability)

Design Decisions:
    - Synchronous Lambda-to-Lambda for prediction steps (need results before proceeding)
    - ASYNC Step Functions StartExecution for generation (fire-and-forget, SFN handles retries)
    - Circuit breaker: >20 counties in one run triggers human review flag
    - Idempotency: DynamoDB alert_state prevents re-alerting same leader/season
    - Plugin architecture: downstream modules subscribe to EventBridge events for enrichment
"""
import json
import os
import sys
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_state_config,
    get_disease_config,
    list_active_states,
    list_active_diseases,
    get_all_sentinel_metros,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- AWS Clients ---
lambda_client = boto3.client("lambda")
sfn_client = boto3.client("stepfunctions")
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
events_client = boto3.client("events")

# --- Configuration ---
system = get_system_config()
DATA_BUCKET = os.environ.get(
    "DATA_BUCKET",
    system["infrastructure"]["data_bucket_name_pattern"]
)
LEADER_DETECTION_FUNCTION = os.environ.get(
    "LEADER_DETECTION_FUNCTION", "healthsignals-leader-detection"
)
GEO_AFFINITY_FUNCTION = os.environ.get(
    "GEO_AFFINITY_FUNCTION", "healthsignals-geographic-affinity"
)
TIMING_ESTIMATION_FUNCTION = os.environ.get(
    "TIMING_ESTIMATION_FUNCTION", "healthsignals-timing-estimation"
)
STATE_MACHINE_ARN = os.environ.get(
    "STATE_MACHINE_ARN", ""
)
PIPELINE_RUNS_TABLE = os.environ.get(
    "PIPELINE_RUNS_TABLE", "healthsignals-pipeline-runs"
)
ALERT_STATE_TABLE = os.environ.get(
    "ALERT_STATE_TABLE", system["dynamodb_tables"]["alert_state"]
)
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")

# Circuit breaker threshold
MAX_COUNTIES_PER_RUN = int(os.environ.get("MAX_COUNTIES_PER_RUN", "20"))

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
alert_state_table = dynamodb.Table(ALERT_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Main handler — triggered by S3 event when new Delphi data lands.

    Can also be invoked directly for testing with a synthetic event:
    {
        "source": "manual",
        "state_key": "texas",
        "disease_key": "influenza",
        "week": "202645"
    }
    """
    execution_id = str(uuid.uuid4())
    start_time = datetime.utcnow()

    logger.info(f"Pipeline execution started: {execution_id}")

    # --- Parse trigger event ---
    if "Records" in event:
        # S3 event notification
        trigger_info = parse_s3_event(event)
    elif event.get("source") == "manual":
        # Manual/test invocation
        trigger_info = {
            "state_key": event.get("state_key"),
            "disease_key": event.get("disease_key"),
            "week": event.get("week"),
            "trigger_type": "manual",
        }
    else:
        return {"statusCode": 400, "error": "Unknown event format"}

    logger.info(f"Trigger info: {json.dumps(trigger_info)}")

    # --- Determine scope ---
    state_key = trigger_info.get("state_key")
    disease_key = trigger_info.get("disease_key")
    week = trigger_info.get("week")

    # If disease not specified, run for ALL active diseases
    diseases_to_check = []
    if disease_key:
        diseases_to_check = [disease_key]
    else:
        diseases_to_check = [d["disease_key"] for d in list_active_diseases()]

    # If state not specified, run for ALL active states
    states_to_check = []
    if state_key:
        states_to_check = [state_key]
    else:
        states_to_check = [s["state_key"] for s in list_active_states()]

    # --- Execute pipeline for each state/disease combination ---
    pipeline_results = {
        "execution_id": execution_id,
        "trigger": trigger_info,
        "detections": [],
        "alerts_triggered": 0,
        "sfn_executions": [],
        "errors": [],
        "circuit_breaker_triggered": False,
    }

    total_counties_alerted = 0

    for state in states_to_check:
        for disease in diseases_to_check:
            try:
                result = run_detection_pipeline(
                    state_key=state,
                    disease_key=disease,
                    week=week,
                    execution_id=execution_id,
                )
                pipeline_results["detections"].append(result)

                if result.get("new_alert") and result.get("counties_alerted"):
                    counties_alerted = result["counties_alerted"]
                    total_counties_alerted += len(counties_alerted)

                    # --- Circuit Breaker ---
                    if total_counties_alerted > MAX_COUNTIES_PER_RUN:
                        logger.warning(
                            f"CIRCUIT BREAKER: {total_counties_alerted} counties alerted "
                            f"exceeds threshold of {MAX_COUNTIES_PER_RUN}. "
                            f"Flagging for human review."
                        )
                        pipeline_results["circuit_breaker_triggered"] = True
                        # Continue processing but flag it

                    # Start Step Functions for each county
                    for county_alert in counties_alerted:
                        try:
                            sfn_execution = start_alert_generation(
                                county_alert=county_alert,
                                execution_id=execution_id,
                            )
                            pipeline_results["sfn_executions"].append(sfn_execution)
                            pipeline_results["alerts_triggered"] += 1
                        except Exception as e:
                            error = f"SFN start failed for {county_alert.get('county_name')}: {e}"
                            pipeline_results["errors"].append(error)
                            logger.error(error)

                    # Emit EventBridge event for downstream plugin modules
                    # (e.g., Drug Shortage enrichment for combined alerts)
                    emit_disease_threshold_event(
                        disease_key=disease,
                        state_key=state,
                        week=week or get_current_epiweek(),
                        leader=result.get("leader", {}),
                        county_alerts=counties_alerted,
                    )

            except Exception as e:
                error = f"Pipeline failed for {state}/{disease}: {str(e)}"
                pipeline_results["errors"].append(error)
                logger.error(error, exc_info=True)

    # --- Record pipeline execution ---
    end_time = datetime.utcnow()
    pipeline_results["duration_seconds"] = (end_time - start_time).total_seconds()
    record_pipeline_execution(execution_id, start_time, pipeline_results)

    logger.info(
        f"Pipeline {execution_id} complete: "
        f"{pipeline_results['alerts_triggered']} alerts triggered, "
        f"{len(pipeline_results['errors'])} errors"
    )

    return {
        "statusCode": 200 if not pipeline_results["errors"] else 207,
        "body": json.dumps(pipeline_results, default=str),
    }


def parse_s3_event(event: dict) -> dict:
    """Parse S3 PutObject event to extract state, disease, and week.

    S3 key pattern from Delphi fetcher:
        raw/delphi/{data_source}/{signal}/{year}/W{week}/{msa_code}.json
    Example:
        raw/delphi/nssp/pct_ed_visits_influenza/2026/W27/26420.json
    """
    record = event["Records"][0]
    s3_key = record["s3"]["object"]["key"]
    bucket = record["s3"]["bucket"]["name"]

    logger.info(f"S3 trigger: s3://{bucket}/{s3_key}")

    parts = s3_key.split("/")
    # Expected: ["raw", "delphi", "nssp", "pct_ed_visits_influenza", "2026", "W27", "26420.json"]

    trigger_info = {
        "trigger_type": "s3_event",
        "s3_bucket": bucket,
        "s3_key": s3_key,
        "state_key": None,  # Will be resolved from MSA code
        "disease_key": None,
        "week": None,
    }

    if len(parts) >= 7:
        signal = parts[3]  # e.g., "pct_ed_visits_influenza"
        year = parts[4]  # e.g., "2026"
        week_str = parts[5]  # e.g., "W27"
        msa_file = parts[6]  # e.g., "26420.json"

        # Map signal name to disease key
        trigger_info["disease_key"] = signal_to_disease(signal)
        trigger_info["week"] = f"{year}{week_str.replace('W', '')}"

        # Resolve MSA code to state
        msa_code = msa_file.replace(".json", "")
        all_metros = get_all_sentinel_metros()
        if msa_code in all_metros:
            trigger_info["state_key"] = all_metros[msa_code].get("state_key")

    return trigger_info


def signal_to_disease(signal_name: str) -> Optional[str]:
    """Map Delphi signal name to disease key.

    Examples:
        pct_ed_visits_influenza → influenza
        pct_ed_visits_covid → covid
        pct_ed_visits_rsv → rsv
    """
    mapping = {
        "pct_ed_visits_influenza": "influenza",
        "smoothed_pct_ed_visits_influenza": "influenza",
        "pct_ed_visits_covid": "covid",
        "smoothed_pct_ed_visits_covid": "covid",
        "pct_ed_visits_rsv": "rsv",
        "smoothed_pct_ed_visits_rsv": "rsv",
    }
    return mapping.get(signal_name)


def run_detection_pipeline(
    state_key: str,
    disease_key: str,
    week: Optional[str],
    execution_id: str,
) -> dict:
    """Run the detection pipeline for a single state/disease combination.

    1. Load latest metro signals from S3
    2. Invoke leader_detection
    3. If leader detected, invoke geographic_affinity + timing_estimation
    """
    logger.info(f"Running detection: {state_key}/{disease_key} (week={week})")

    # --- Step 1: Load latest metro signals ---
    metro_signals = load_latest_metro_signals(state_key, disease_key)

    if not metro_signals:
        return {
            "state": state_key,
            "disease": disease_key,
            "detected": False,
            "reason": "No metro signals available",
        }

    # --- Step 2: Leader Detection ---
    detection_payload = {
        "disease": disease_key,
        "state_key": state_key,
        "week": week or get_current_epiweek(),
        "metro_signals": metro_signals,
    }

    detection_result = invoke_lambda_sync(
        LEADER_DETECTION_FUNCTION, detection_payload
    )

    if not detection_result.get("detected") or not detection_result.get("new_alert"):
        return {
            "state": state_key,
            "disease": disease_key,
            "detected": detection_result.get("detected", False),
            "new_alert": False,
            "reason": detection_result.get("reason", "No new alert"),
            "leader": detection_result.get("leader"),
        }

    # --- Step 3: Geographic Affinity ---
    leader = detection_result["leader"]
    affinity_payload = {
        "leader": leader,
        "disease": disease_key,
        "state_key": state_key,
        "week": detection_payload["week"],
    }

    affinity_result = invoke_lambda_sync(GEO_AFFINITY_FUNCTION, affinity_payload)
    affected_counties = affinity_result.get("affected_counties", [])

    if not affected_counties:
        return {
            "state": state_key,
            "disease": disease_key,
            "detected": True,
            "new_alert": True,
            "leader": leader,
            "counties_alerted": [],
            "reason": "Leader detected but no subscribing counties affected",
        }

    # --- Step 4: Timing Estimation for each county ---
    counties_with_timing = []
    for county in affected_counties:
        timing_payload = {
            "leader_msa": leader["msa_code"],
            "disease": disease_key,
            "week": detection_payload["week"],
            "county_fips": county["county_fips"],
            "county_name": county["county_name"],
            "affinity_weight": county.get("affinity_weight", 1.0),
            "state_key": state_key,
        }

        timing_result = invoke_lambda_sync(TIMING_ESTIMATION_FUNCTION, timing_payload)

        county_alert = {
            **county,
            "disease": disease_key,
            "state_key": state_key,
            "leader_metro_name": leader.get("metro_name", leader["msa_code"]),
            "leader_msa_code": leader["msa_code"],
            "leader_value": leader["value"],
            "detection_week": detection_payload["week"],
            "lag_weeks": timing_result.get("estimated_lag_weeks", 4),
            "severity_multiplier": timing_result.get("severity_multiplier", 1.5),
            "confidence": timing_result.get("confidence", 0.6),
            "seasons_calibrated": timing_result.get("seasons_calibrated", 3),
            "warning_window_weeks": timing_result.get("warning_window_weeks", 4),
            "cdc_activity_level": timing_result.get("cdc_activity_level", "unknown"),
            "execution_id": execution_id,
            "external_forecast": timing_result.get("external_forecast"),
        }
        counties_with_timing.append(county_alert)

    # Set alert type for all county alerts (disease_outbreak only — enrichment
    # with shortage context is handled by downstream modules via EventBridge)
    for county_alert in counties_with_timing:
        county_alert["alert_type"] = "disease_outbreak"

    return {
        "state": state_key,
        "disease": disease_key,
        "detected": True,
        "new_alert": True,
        "leader": leader,
        "counties_alerted": counties_with_timing,
    }


def load_latest_metro_signals(state_key: str, disease_key: str) -> dict:
    """Load the latest metro signals for a state/disease from S3.

    Scans the S3 data lake for the most recent Delphi fetch data
    and aggregates metro signal values into the format expected by
    leader_detection.

    Returns:
        dict mapping MSA code → {"value": float, "trend": str}
    """
    state_config = get_state_config(state_key)
    disease_config = get_disease_config(disease_key)
    metros = state_config.get("sentinel_metros", {})

    # Get the Delphi signal name for this disease
    delphi_config = disease_config.get("data_sources", {}).get("delphi", {})
    data_source = delphi_config.get("data_source", "nssp")
    signal_name = delphi_config.get("signal", "")

    if not signal_name:
        logger.warning(f"No Delphi signal configured for {disease_key}")
        return {}

    metro_signals = {}

    for msa_code, metro_info in metros.items():
        # Use primary_county_fips for S3 lookup (data is saved by county FIPS, not MSA code)
        geo_value = metro_info.get("primary_county_fips", metro_info.get("county_fips", [msa_code])[0])
        try:
            # Find the most recent data file for this metro
            prefix = f"raw/delphi/{data_source}/{signal_name}/"
            response = s3_client.list_objects_v2(
                Bucket=DATA_BUCKET,
                Prefix=prefix,
                Delimiter="/",
            )

            # Get the most recent year/week folder
            common_prefixes = response.get("CommonPrefixes", [])
            if not common_prefixes:
                continue

            # List week folders in the most recent year
            year_prefixes = sorted(
                [p["Prefix"] for p in common_prefixes], reverse=True
            )
            latest_year_prefix = year_prefixes[0]

            # Get week folders
            week_response = s3_client.list_objects_v2(
                Bucket=DATA_BUCKET,
                Prefix=latest_year_prefix,
                Delimiter="/",
            )
            week_prefixes = sorted(
                [p["Prefix"] for p in week_response.get("CommonPrefixes", [])],
                reverse=True,
            )

            if not week_prefixes:
                continue

            # Read the latest data file (keyed by county FIPS, not MSA code)
            data_key = f"{week_prefixes[0]}{geo_value}.json"
            obj = s3_client.get_object(Bucket=DATA_BUCKET, Key=data_key)
            raw_data = json.loads(obj["Body"].read().decode())

            # Extract the latest signal value
            signal_value, trend = extract_latest_signal(raw_data)

            if signal_value is not None:
                metro_signals[msa_code] = {
                    "value": signal_value,
                    "trend": trend,
                }

        except s3_client.exceptions.NoSuchKey:
            logger.warning(f"No data found for MSA {msa_code}/{signal_name}")
        except Exception as e:
            logger.error(f"Error loading data for MSA {msa_code}: {e}")

    return metro_signals


def extract_latest_signal(raw_data: dict) -> tuple:
    """Extract the most recent signal value and trend from Delphi API response.

    Returns:
        (value: float, trend: str) — trend is "rising", "declining", or "stable"
    """
    epidata = raw_data.get("epidata", [])
    if not epidata:
        return None, "unknown"

    # Sort by time_value descending to get most recent
    sorted_data = sorted(epidata, key=lambda x: x.get("time_value", 0), reverse=True)

    latest = sorted_data[0]
    value = latest.get("value")

    if value is None:
        return None, "unknown"

    # Determine trend from last 3 data points
    if len(sorted_data) >= 3:
        values = [d.get("value", 0) for d in sorted_data[:3]]
        # values[0] = most recent, values[2] = 3 periods ago
        if values[0] > values[1] > values[2]:
            trend = "rising"
        elif values[0] < values[1] < values[2]:
            trend = "declining"
        elif values[0] > values[2]:
            trend = "rising"
        elif values[0] < values[2]:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "unknown"

    return float(value), trend


def invoke_lambda_sync(function_name: str, payload: dict) -> dict:
    """Invoke a Lambda function synchronously and return the parsed response.

    Raises:
        RuntimeError if the invocation fails or returns an error.
    """
    logger.info(f"Invoking {function_name}")

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )

    # Check for invocation errors
    if response.get("FunctionError"):
        error_payload = response["Payload"].read().decode()
        raise RuntimeError(
            f"Lambda {function_name} returned error: {error_payload[:500]}"
        )

    result_payload = json.loads(response["Payload"].read().decode())

    # Handle case where Lambda returns {"statusCode": ..., "body": "..."}
    if "body" in result_payload and isinstance(result_payload["body"], str):
        try:
            return json.loads(result_payload["body"])
        except json.JSONDecodeError:
            return result_payload

    return result_payload


def start_alert_generation(county_alert: dict, execution_id: str) -> dict:
    """Start a Step Functions execution for one county alert.

    The Step Functions state machine handles the 4-step Bedrock generation:
    1. Situation Brief
    2. Severity Classification
    3. Preparation Checklist
    4. Communication Drafting

    Returns:
        dict with execution ARN and start timestamp.
    """
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not set — skipping SFN execution")
        return {"skipped": True, "reason": "STATE_MACHINE_ARN not configured"}

    # Build unique execution name (must be unique within 90 days)
    county_fips = county_alert.get("county_fips", "unknown")
    disease = county_alert.get("disease", "unknown")
    week = county_alert.get("detection_week", "unknown")
    exec_name = f"{county_fips}-{disease}-{week}-{execution_id[:8]}"
    # SFN execution names max 80 chars, must match [a-zA-Z0-9-_]
    exec_name = exec_name[:80].replace(" ", "_")

    # Prepare Step Functions input
    confidence = county_alert.get("confidence", 0.6)

    sfn_input = {
        "county_fips": county_fips,
        "county_name": county_alert.get("county_name", "Unknown County"),
        "disease": disease,
        "leader_metro_name": county_alert.get("leader_metro_name", "Unknown"),
        "leader_value": county_alert.get("leader_value", 0),
        "detection_week": week,
        "lag_weeks": county_alert.get("lag_weeks", 4),
        "severity_multiplier": county_alert.get("severity_multiplier", 1.5),
        "confidence": confidence,
        "confidence_pct": int(confidence * 100),
        "seasons_calibrated": county_alert.get("seasons_calibrated", 3),
        "warning_window_weeks": county_alert.get("warning_window_weeks", 4),
        "cdc_activity_level": county_alert.get("cdc_activity_level", "unknown"),
        "alert_contacts": county_alert.get("alert_contacts", []),
        "alert_type": county_alert.get("alert_type", "disease_outbreak"),
        "execution_id": execution_id,
        "external_forecast": county_alert.get("external_forecast"),
    }

    response = sfn_client.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=exec_name,
        input=json.dumps(sfn_input),
    )

    logger.info(
        f"Started SFN execution: {exec_name} for {county_alert.get('county_name')}"
    )

    return {
        "execution_arn": response["executionArn"],
        "execution_name": exec_name,
        "county": county_alert.get("county_name"),
        "disease": disease,
        "started_at": response["startDate"].isoformat(),
    }


def record_pipeline_execution(
    execution_id: str, start_time: datetime, results: dict
) -> None:
    """Record pipeline execution to DynamoDB for observability.

    Table: healthsignals-pipeline-runs
    PK: execution_id
    """
    try:
        item = {
            "execution_id": execution_id,
            "started_at": start_time.isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "duration_seconds": str(results.get("duration_seconds", 0)),
            "trigger_type": results.get("trigger", {}).get("trigger_type", "unknown"),
            "alerts_triggered": results["alerts_triggered"],
            "detections_count": len(results["detections"]),
            "errors_count": len(results["errors"]),
            "circuit_breaker_triggered": results["circuit_breaker_triggered"],
            "sfn_executions_count": len(results["sfn_executions"]),
        }

        # Store errors for debugging (truncated to DynamoDB item size limits)
        if results["errors"]:
            item["errors"] = results["errors"][:10]  # Max 10 errors

        pipeline_runs_table.put_item(Item=item)
        logger.info(f"Recorded pipeline execution: {execution_id}")

    except Exception as e:
        # Don't fail the pipeline if we can't record metrics
        logger.error(f"Failed to record pipeline execution: {e}")


def get_current_epiweek() -> str:
    """Get the current epidemiological week as YYYYWW format."""
    now = datetime.utcnow()
    year = now.strftime("%Y")
    week = now.strftime("%W")
    return f"{year}{week}"


def emit_disease_threshold_event(
    disease_key: str,
    state_key: str,
    week: str,
    leader: dict,
    county_alerts: list[dict],
) -> None:
    """Emit an EventBridge event when a disease threshold is crossed.

    Downstream plugin modules (e.g., Drug Shortage Intelligence) subscribe
    to this event to enrich alerts with additional context.

    Event detail-type: healthsignals.disease.threshold_crossed
    Source: healthsignals.pipeline_coordinator
    """
    detail = {
        "disease_key": disease_key,
        "state_key": state_key,
        "week": week,
        "leader": leader,
        "county_alerts": county_alerts,
        "emitted_at": datetime.utcnow().isoformat(),
    }

    try:
        events_client.put_events(
            Entries=[
                {
                    "Source": "healthsignals.pipeline_coordinator",
                    "DetailType": "healthsignals.disease.threshold_crossed",
                    "Detail": json.dumps(detail, default=str),
                    "EventBusName": EVENT_BUS_NAME,
                }
            ]
        )
        logger.info(
            f"Emitted threshold_crossed event for {disease_key}/{state_key} "
            f"with {len(county_alerts)} county alerts"
        )
    except Exception as e:
        # Don't fail the pipeline if EventBridge emission fails
        logger.error(f"Failed to emit EventBridge event: {e}")

