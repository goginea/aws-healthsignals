"""Outbreak Processor — Routes CDC outbreak alerts to subscribing counties.

Triggered by: CDC Outbreak Fetcher (async Lambda invoke) with extracted outbreak data.

Flow:
    1. Normalize state names from Bedrock extraction (full name → state key)
    2. Resolve affected states to subscribing counties
    3. Identify newly added states (for update alerts)
    4. Start Step Functions execution for alert generation per affected state

Design:
    - No leader detection (CDC notification IS the detection)
    - No geographic affinity (affected states explicitly listed)
    - No timing estimation (outbreak is already happening)
    - State-level alerting: all subscribing counties in affected states get alerts

Environment Variables:
    STATE_MACHINE_ARN: Step Functions ARN for outbreak alert generation
    CONFIG_BUCKET: S3 bucket for config files
    CONFIG_PREFIX: S3 key prefix for configs
    LOG_LEVEL: Logging level
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

# Add shared module to path
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import list_active_states, get_state_config
from shared.geo_utils import normalize_state_names

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

sfn_client = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")


def lambda_handler(event: dict, context: Any) -> dict:
    """Process extracted outbreak data and start alert generation.

    Input event (from CDC Outbreak Fetcher):
    {
        "outbreak_id": "cyclosporiasis-outbreak-...",
        "title": "Cyclosporiasis Outbreak with Unknown Source",
        "disease_name": "Cyclosporiasis",
        "affected_states": ["Michigan", "Ohio", "West Virginia", "Kentucky"],
        "case_count": 400,
        "hospitalizations": null,
        "deaths": null,
        "source_food": "Unknown",
        "onset_date": "2026-06-22",
        "status": "active",
        "summary": "...",
        "cdc_link": "https://...",
        "pub_date": "...",
        "category": "Outbreaks",
        "previous_states": ["Michigan", "Ohio"],  // empty for new outbreaks
        "fetched_at": "..."
    }
    """
    logger.info(f"Outbreak Processor invoked: {event.get('title')}")

    outbreak_id = event.get("outbreak_id")
    title = event.get("title", "Unknown Outbreak")
    affected_states_raw = event.get("affected_states", [])
    previous_states_raw = event.get("previous_states", [])

    if not affected_states_raw:
        logger.warning(f"No affected states for outbreak: {title}")
        return {"statusCode": 200, "alerts_started": 0, "reason": "no_affected_states"}

    # 1. Normalize state names
    affected_states = normalize_state_names(affected_states_raw)
    previous_states = normalize_state_names(previous_states_raw)

    # 2. Identify newly added states
    new_states = [s for s in affected_states if s not in previous_states]

    logger.info(
        f"Outbreak '{title}': {len(affected_states)} affected states "
        f"({len(new_states)} newly added)"
    )

    # 3. Resolve to subscribing counties and start SFN per state
    alerts_started = 0
    errors = []

    # Get list of states we actually monitor
    monitored_states = {s["state_key"] for s in list_active_states()}

    for state_key in affected_states:
        if state_key not in monitored_states:
            logger.info(f"State '{state_key}' not monitored, skipping")
            continue

        try:
            # Get subscribing counties for this state
            state_config = get_state_config(state_key)
            counties = state_config.get("subscribing_counties", [])

            if not counties:
                logger.info(f"No subscribing counties for state '{state_key}', skipping")
                continue

            # Build Step Functions input
            sfn_input = {
                "alert_type": "cdc_outbreak",
                "outbreak_id": outbreak_id,
                "title": title,
                "disease_name": event.get("disease_name", "Unknown"),
                "affected_states": affected_states,
                "new_states": new_states,
                "is_update": len(previous_states) > 0,
                "state_key": state_key,
                "counties": counties,
                "case_count": event.get("case_count"),
                "hospitalizations": event.get("hospitalizations"),
                "deaths": event.get("deaths"),
                "source_food": event.get("source_food", "Unknown"),
                "onset_date": event.get("onset_date"),
                "status": event.get("status", "active"),
                "summary": event.get("summary", ""),
                "cdc_link": event.get("cdc_link", ""),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }

            execution_arn = start_alert_generation(sfn_input)
            if execution_arn:
                alerts_started += 1

        except Exception as e:
            error_msg = f"Failed to process state '{state_key}': {e}"
            logger.error(error_msg, exc_info=True)
            errors.append(error_msg)

    result = {
        "statusCode": 200,
        "outbreak_id": outbreak_id,
        "title": title,
        "affected_states": affected_states,
        "new_states": new_states,
        "alerts_started": alerts_started,
        "errors": errors[:5],
    }

    logger.info(f"Processor complete: {json.dumps(result, default=str)}")
    return result


def start_alert_generation(sfn_input: dict) -> str | None:
    """Start a Step Functions execution for one state's outbreak alert.

    Returns execution ARN or None on failure.
    """
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not configured — skipping SFN execution")
        return None

    outbreak_id = sfn_input["outbreak_id"]
    state_key = sfn_input["state_key"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    # Execution name must be unique, <=80 chars, [a-zA-Z0-9-_]
    exec_name = f"outbreak-{state_key}-{outbreak_id[:30]}-{timestamp}"
    exec_name = exec_name[:80].replace(" ", "-")

    try:
        response = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(sfn_input, default=str),
        )
        logger.info(f"Started SFN: {exec_name}")
        return response["executionArn"]

    except Exception as e:
        logger.error(f"SFN start failed for {state_key}: {e}")
        return None

