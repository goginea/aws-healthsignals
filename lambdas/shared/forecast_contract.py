"""Forecast Contract — Shared validation and normalization for forecast data.

All forecast providers (FluSight, RSV Hub, custom models) must output data
conforming to the Standard Forecast Contract. This module validates and
normalizes forecast records before they are written to DynamoDB.

Usage:
    from shared.forecast_contract import validate_forecast, normalize_forecast_geo

    data = {...}  # from provider
    if validate_forecast(data):
        normalized = normalize_forecast_geo(data)
        # write to DynamoDB
"""
import logging
from typing import Optional

from shared.geo_utils import normalize_state_name

logger = logging.getLogger(__name__)

# Required top-level fields in the Standard Forecast Contract
REQUIRED_FIELDS = [
    "provider",
    "disease",
    "geo_level",
    "geo_value",
    "forecast_date",
    "target",
    "predictions",
]

# Required fields per prediction horizon entry
REQUIRED_PREDICTION_FIELDS = [
    "horizon_weeks",
    "point_estimate",
]

VALID_GEO_LEVELS = {"state", "national"}
VALID_TARGETS = {"hospitalizations", "ed_visits", "cases"}


def validate_forecast(data: dict) -> bool:
    """Validate a forecast record against the Standard Forecast Contract.

    Args:
        data: Forecast dict from a provider (API response or parsed CSV).

    Returns:
        True if valid, False if missing required fields or invalid structure.
    """
    # Check required top-level fields
    for field in REQUIRED_FIELDS:
        if field not in data or data[field] is None:
            logger.warning(f"Forecast validation failed: missing field '{field}'")
            return False

    # Validate geo_level
    if data["geo_level"] not in VALID_GEO_LEVELS:
        logger.warning(f"Forecast validation failed: invalid geo_level '{data['geo_level']}'")
        return False

    # Validate target
    if data["target"] not in VALID_TARGETS:
        logger.warning(f"Forecast validation failed: invalid target '{data['target']}'")
        return False

    # Validate predictions array
    predictions = data["predictions"]
    if not isinstance(predictions, list) or len(predictions) == 0:
        logger.warning("Forecast validation failed: predictions must be a non-empty array")
        return False

    for i, pred in enumerate(predictions):
        for field in REQUIRED_PREDICTION_FIELDS:
            if field not in pred or pred[field] is None:
                logger.warning(
                    f"Forecast validation failed: predictions[{i}] missing '{field}'"
                )
                return False

        # horizon_weeks must be positive integer
        if not isinstance(pred["horizon_weeks"], int) or pred["horizon_weeks"] < 1:
            logger.warning(
                f"Forecast validation failed: predictions[{i}].horizon_weeks must be positive integer"
            )
            return False

        # point_estimate must be numeric
        if not isinstance(pred["point_estimate"], (int, float)):
            logger.warning(
                f"Forecast validation failed: predictions[{i}].point_estimate must be numeric"
            )
            return False

    return True


def normalize_forecast_geo(data: dict) -> dict:
    """Normalize the geo_value in a forecast record to internal state key format.

    Converts state abbreviations (TX) and full names (Texas) to the internal
    format (texas) using shared/geo_utils.

    For national-level forecasts (geo_value = "US"), keeps as-is.

    Args:
        data: Validated forecast dict.

    Returns:
        Same dict with geo_value normalized. Returns unchanged if normalization fails.
    """
    geo_level = data.get("geo_level", "")
    geo_value = data.get("geo_value", "")

    if geo_level == "national":
        # National forecasts use "US" — no normalization needed
        data["geo_value"] = "US"
        return data

    if geo_level == "state":
        normalized = normalize_state_name(geo_value)
        if normalized:
            data["geo_value"] = normalized
        else:
            logger.warning(f"Could not normalize geo_value '{geo_value}' — keeping as-is")

    return data


def build_dynamodb_key(geo_value: str, disease: str, week: str) -> dict:
    """Build the DynamoDB key for the forecast-state table.

    Args:
        geo_value: Normalized state key (e.g., "texas") or "US" for national.
        disease: Disease key (e.g., "influenza").
        week: ISO week string (e.g., "2026-W45").

    Returns:
        Dict with geo_key (PK) and disease_week (SK).
    """
    return {
        "geo_key": geo_value,
        "disease_week": f"{disease}_{week}",
    }


def forecast_to_dynamodb_item(data: dict, week: str) -> dict:
    """Convert a validated, normalized forecast to a DynamoDB item.

    Args:
        data: Validated and geo-normalized forecast dict.
        week: Current ISO week (e.g., "2026-W45").

    Returns:
        DynamoDB item dict ready for put_item.
    """
    import time

    geo_value = data["geo_value"]
    disease = data["disease"]

    # TTL: 8 weeks from now
    ttl_value = int(time.time()) + (8 * 7 * 24 * 3600)

    item = {
        "geo_key": geo_value,
        "disease_week": f"{disease}_{week}",
        "provider": data["provider"],
        "disease": disease,
        "geo_level": data["geo_level"],
        "forecast_date": data["forecast_date"],
        "target": data["target"],
        "predictions": data["predictions"],
        "trust_weight": data.get("trust_weight", 0.7),
        "metadata": data.get("metadata", {}),
        "ttl": ttl_value,
    }

    return item
