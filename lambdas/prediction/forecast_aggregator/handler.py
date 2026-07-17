"""Forecast Aggregator — Combines forecasts from multiple providers with weighted averaging.

Triggered after ingestion Lambdas complete (or on schedule). For each disease + state + week:
collects all provider forecasts from DynamoDB, computes weighted mean of point estimates,
blends quantiles, detects conflicts, writes aggregated result, and emits EventBridge event.

Environment Variables:
    FORECAST_STATE_TABLE: DynamoDB table with per-provider forecasts
    EVENT_BUS_NAME: EventBridge bus for publishing forecast.updated events
    CONFIG_BUCKET: S3 bucket for config
    CONFIG_PREFIX: S3 key prefix
    LOG_LEVEL: Logging level
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# Add shared module to path
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import get_system_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
events_client = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")

system = get_system_config()
FORECAST_STATE_TABLE = os.environ.get(
    "FORECAST_STATE_TABLE",
    system.get("forecast_providers", {}).get("forecast_state_table", "healthsignals-forecast-state")
)
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
CONFLICT_THRESHOLD_PCT = int(
    system.get("forecast_providers", {}).get("conflict_threshold_pct", 50)
)

METRIC_NAMESPACE = "HealthSignals/ForecastProviders"

forecast_table = dynamodb.Table(FORECAST_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Aggregate forecasts for specified disease+state+week or all recent entries.

    Input event:
    {
        "disease": "influenza",       // optional — if omitted, aggregate all
        "geo_key": "texas",           // optional — if omitted, aggregate all states
        "week": "2026-W45"            // optional — if omitted, use current week
    }
    """
    logger.info(f"Forecast Aggregator invoked: {json.dumps(event)}")

    disease = event.get("disease")
    geo_key = event.get("geo_key")
    week = event.get("week") or get_current_iso_week()

    # Determine what to aggregate
    if disease and geo_key:
        # Single state+disease
        targets = [(geo_key, disease, week)]
    else:
        # Scan for all recent entries in the table for this week
        targets = find_aggregation_targets(week)

    aggregated_count = 0
    conflicts_detected = 0

    for target_geo, target_disease, target_week in targets:
        disease_week = f"{target_disease}_{target_week}"

        # Get all provider forecasts for this geo+disease+week
        forecasts = get_provider_forecasts(target_geo, disease_week)

        if len(forecasts) < 1:
            continue

        # Aggregate
        result = aggregate_forecasts(forecasts, target_geo, target_disease, target_week)

        if result.get("conflict"):
            conflicts_detected += 1

        # Write aggregated result to DynamoDB
        write_aggregated_result(result)
        aggregated_count += 1

        # Emit EventBridge event
        emit_forecast_updated_event(result)

    emit_metrics(aggregated_count, conflicts_detected)

    return {
        "statusCode": 200,
        "aggregated": aggregated_count,
        "conflicts_detected": conflicts_detected,
        "week": week,
    }


def find_aggregation_targets(week: str) -> list[tuple]:
    """Scan DynamoDB for all unique (geo_key, disease) combinations for this week.

    Returns list of (geo_key, disease, week) tuples.
    """
    targets = set()
    try:
        # Scan with filter for entries containing this week in the sort key
        response = forecast_table.scan(
            FilterExpression=Key("disease_week").begins_with("") & Key("disease_week").begins_with("")
        )
        # Simpler: scan all and filter client-side
        response = forecast_table.scan()
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = forecast_table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        for item in items:
            disease_week_val = item.get("disease_week", "")
            if week in disease_week_val:
                geo_key = item.get("geo_key", "")
                disease = item.get("disease", "")
                if geo_key and disease:
                    targets.add((geo_key, disease, week))

    except Exception as e:
        logger.error(f"Scan for aggregation targets failed: {e}")

    return list(targets)


def get_provider_forecasts(geo_key: str, disease_week: str) -> list[dict]:
    """Query DynamoDB for all provider forecasts matching geo_key + disease_week.

    Since multiple providers write to the same PK+SK, we use a provider-specific
    sort key: disease_week is actually disease_week#provider for per-provider records.

    For simplicity in MVP: scan with filter on geo_key and disease_week prefix.
    """
    try:
        response = forecast_table.query(
            KeyConditionExpression=(
                Key("geo_key").eq(geo_key)
                & Key("disease_week").begins_with(disease_week.split("_")[0])
            ),
            FilterExpression=Key("disease_week").begins_with(disease_week.split("_")[0])
        )
        items = response.get("Items", [])

        # Filter to only matching disease+week entries
        disease, week_part = disease_week.split("_", 1)
        matching = [
            item for item in items
            if item.get("disease") == disease
            and week_part in item.get("disease_week", "")
        ]

        return matching

    except Exception as e:
        logger.error(f"Query failed for {geo_key}/{disease_week}: {e}")
        return []


def aggregate_forecasts(
    forecasts: list[dict], geo_key: str, disease: str, week: str
) -> dict:
    """Aggregate multiple provider forecasts into a single weighted result.

    Uses weighted mean for point estimates, weighted average for quantiles,
    and detects conflicts when providers disagree by more than the threshold.
    """
    if len(forecasts) == 1:
        # Single provider — no aggregation needed
        f = forecasts[0]
        return {
            "geo_key": geo_key,
            "disease": disease,
            "week": week,
            "aggregated": True,
            "provider_count": 1,
            "providers": [f.get("provider", "unknown")],
            "predictions": f.get("predictions", []),
            "target": f.get("target", "hospitalizations"),
            "conflict": False,
            "agreement_status": "single_source",
        }

    # Collect predictions by horizon
    horizon_data: dict[int, list] = {}  # horizon → list of (point_estimate, weight, quantiles)

    total_weight = 0
    providers = []

    for f in forecasts:
        weight = float(f.get("trust_weight", 0.7))
        provider = f.get("provider", "unknown")
        providers.append(provider)
        total_weight += weight

        for pred in f.get("predictions", []):
            horizon = pred.get("horizon_weeks", 0)
            if horizon not in horizon_data:
                horizon_data[horizon] = []
            horizon_data[horizon].append({
                "point_estimate": pred.get("point_estimate", 0),
                "weight": weight,
                "quantiles": pred.get("quantiles", {}),
            })

    # Compute weighted aggregation per horizon
    aggregated_predictions = []
    conflict = False

    for horizon in sorted(horizon_data.keys()):
        entries = horizon_data[horizon]

        # Weighted mean of point estimates
        weighted_sum = sum(e["point_estimate"] * e["weight"] for e in entries)
        weight_sum = sum(e["weight"] for e in entries)
        point_estimate = weighted_sum / weight_sum if weight_sum > 0 else 0

        # Check for conflict (providers disagree by > threshold)
        estimates = [e["point_estimate"] for e in entries if e["point_estimate"] > 0]
        if len(estimates) >= 2:
            max_est = max(estimates)
            min_est = min(estimates)
            if min_est > 0:
                pct_diff = ((max_est - min_est) / min_est) * 100
                if pct_diff > CONFLICT_THRESHOLD_PCT:
                    conflict = True

        # Blend quantiles (weighted average per quantile level)
        blended_quantiles = {}
        quantile_keys = set()
        for e in entries:
            quantile_keys.update(e.get("quantiles", {}).keys())

        for q_key in sorted(quantile_keys):
            q_weighted_sum = 0
            q_weight_sum = 0
            for e in entries:
                q_val = e.get("quantiles", {}).get(q_key)
                if q_val is not None:
                    q_weighted_sum += float(q_val) * e["weight"]
                    q_weight_sum += e["weight"]
            if q_weight_sum > 0:
                blended_quantiles[q_key] = round(q_weighted_sum / q_weight_sum, 2)

        pred = {
            "horizon_weeks": horizon,
            "point_estimate": round(point_estimate, 2),
        }
        if blended_quantiles:
            pred["quantiles"] = blended_quantiles

        aggregated_predictions.append(pred)

    # Determine agreement status
    if conflict:
        agreement_status = "disagreement"
    elif len(forecasts) >= 2:
        agreement_status = "consensus"
    else:
        agreement_status = "single_source"

    return {
        "geo_key": geo_key,
        "disease": disease,
        "week": week,
        "aggregated": True,
        "provider_count": len(forecasts),
        "providers": providers,
        "predictions": aggregated_predictions,
        "target": forecasts[0].get("target", "hospitalizations"),
        "conflict": conflict,
        "agreement_status": agreement_status,
    }


def write_aggregated_result(result: dict) -> None:
    """Write aggregated forecast to DynamoDB with special provider='_aggregated'."""
    import time

    try:
        item = {
            "geo_key": result["geo_key"],
            "disease_week": f"{result['disease']}_{result['week']}",
            "provider": "_aggregated",
            "disease": result["disease"],
            "aggregated": True,
            "provider_count": result["provider_count"],
            "providers": result["providers"],
            "predictions": result["predictions"],
            "target": result["target"],
            "conflict": result["conflict"],
            "agreement_status": result["agreement_status"],
            "aggregated_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(time.time()) + (8 * 7 * 24 * 3600),
        }
        forecast_table.put_item(Item=item)
    except Exception as e:
        logger.error(f"Failed to write aggregated result: {e}")


def emit_forecast_updated_event(result: dict) -> None:
    """Emit healthsignals.forecast.updated EventBridge event."""
    try:
        detail = {
            "geo_key": result["geo_key"],
            "disease": result["disease"],
            "week": result["week"],
            "provider_count": result["provider_count"],
            "conflict": result["conflict"],
            "agreement_status": result["agreement_status"],
            "aggregated_at": datetime.now(timezone.utc).isoformat(),
        }

        events_client.put_events(
            Entries=[{
                "Source": "healthsignals.forecast_provider",
                "DetailType": "healthsignals.forecast.updated",
                "Detail": json.dumps(detail, default=str),
                "EventBusName": EVENT_BUS_NAME,
            }]
        )
    except Exception as e:
        logger.error(f"EventBridge emission failed: {e}")


def get_current_iso_week() -> str:
    """Get current ISO week as YYYY-WNN."""
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def emit_metrics(aggregated: int, conflicts: int) -> None:
    """Emit CloudWatch metrics."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "forecasts_aggregated",
                    "Value": aggregated,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "FunctionName", "Value": "forecast_aggregator"}],
                },
                {
                    "MetricName": "forecast_conflicts",
                    "Value": conflicts,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "FunctionName", "Value": "forecast_aggregator"}],
                },
            ],
        )
    except Exception as e:
        logger.error(f"Metrics emission failed: {e}")
