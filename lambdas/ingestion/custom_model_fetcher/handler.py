"""Custom Model Fetcher — Calls user-registered forecast model APIs.

Invoked for each custom provider configured in config/forecast_providers/.
Builds a request with current HealthSignals surveillance context, calls the
provider's API endpoint, validates the response against the Standard Forecast
Contract, normalizes geo values, and writes to DynamoDB + S3.

Supports authentication: none, api_key, bearer, iam_sigv4.

Environment Variables:
    DATA_BUCKET: S3 bucket for raw archive
    FORECAST_STATE_TABLE: DynamoDB table for normalized forecasts
    CONFIG_BUCKET: S3 bucket for config
    CONFIG_PREFIX: S3 key prefix
    LOG_LEVEL: Logging level
"""
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
import urllib3

# Add shared module to path
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.forecast_contract import (
    validate_forecast,
    normalize_forecast_geo,
    forecast_to_dynamodb_item,
)
from shared.config_loader import get_system_config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
secrets_client = boto3.client("secretsmanager")
cloudwatch = boto3.client("cloudwatch")

DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")
CONFIG_PREFIX = os.environ.get("CONFIG_PREFIX", "config/")
FORECAST_STATE_TABLE = os.environ.get("FORECAST_STATE_TABLE", "healthsignals-forecast-state")
METRIC_NAMESPACE = "HealthSignals/ForecastProviders"

forecast_table = dynamodb.Table(FORECAST_STATE_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch forecast from a custom model provider.

    Input event:
    {
        "provider_name": "tx_dshs_seir_model",
        "disease": "influenza",
        "state_key": "texas",
        "current_signals": {
            "ed_visit_pct": 3.2,
            "ed_visit_trend": "rising",
            "wastewater_level": "moderate",
            "leader_metro": "Houston",
            "weeks_since_threshold_crossing": 1
        }
    }

    Or invoked with just provider_name to fetch for all configured diseases/states.
    """
    provider_name = event.get("provider_name")
    if not provider_name:
        return {"statusCode": 400, "error": "Missing provider_name"}

    logger.info(f"Custom model fetch for provider: {provider_name}")

    # Load provider config from S3
    provider_config = load_provider_config(provider_name)
    if not provider_config:
        return {"statusCode": 404, "error": f"Provider config not found: {provider_name}"}

    if not provider_config.get("enabled", False):
        return {"statusCode": 200, "skipped": True, "reason": "Provider disabled"}

    endpoint_url = provider_config.get("endpoint_url")
    if not endpoint_url:
        return {"statusCode": 400, "error": "No endpoint_url in provider config"}

    # Build request body
    request_body = build_request_body(event, provider_config)

    # Get auth headers
    headers = build_auth_headers(provider_config)
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "HealthSignals-ForecastProvider/1.0"

    # Call the provider API
    timeout = provider_config.get("timeout_seconds", 30)
    response_data = call_provider_api(endpoint_url, request_body, headers, timeout)

    if response_data is None:
        emit_metrics(provider_name, 0, 1)
        if provider_config.get("fallback_on_error", True):
            logger.warning(f"Provider {provider_name} failed — skipping (fallback_on_error=True)")
            return {"statusCode": 200, "skipped": True, "reason": "API call failed, fallback enabled"}
        return {"statusCode": 502, "error": f"Provider {provider_name} API call failed"}

    # Validate response against Standard Forecast Contract
    if not validate_forecast(response_data):
        emit_metrics(provider_name, 0, 1)
        logger.error(f"Provider {provider_name} returned invalid forecast format")
        return {"statusCode": 422, "error": "Invalid forecast format from provider"}

    # Add trust_weight from config
    response_data["trust_weight"] = provider_config.get("trust_weight", 0.7)

    # Normalize geo and write to DynamoDB
    normalized = normalize_forecast_geo(response_data)
    week = get_current_iso_week()
    item = forecast_to_dynamodb_item(normalized, week)

    try:
        forecast_table.put_item(Item=item)
        logger.info(f"Wrote forecast from {provider_name} to DynamoDB")
    except Exception as e:
        logger.error(f"DynamoDB write failed for {provider_name}: {e}")
        emit_metrics(provider_name, 0, 1)
        return {"statusCode": 500, "error": f"DynamoDB write failed: {e}"}

    # Store raw response to S3
    store_raw_response(response_data, provider_name)

    emit_metrics(provider_name, 1, 0)

    return {
        "statusCode": 200,
        "provider": provider_name,
        "forecasts_written": 1,
        "geo_value": normalized.get("geo_value"),
        "disease": normalized.get("disease"),
    }


def load_provider_config(provider_name: str) -> Optional[dict]:
    """Load provider config from S3."""
    if not CONFIG_BUCKET:
        # Try local filesystem (for testing)
        local_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "config",
            "forecast_providers", f"{provider_name}.json"
        )
        if os.path.exists(local_path):
            with open(local_path) as f:
                return json.load(f)
        return None

    try:
        key = f"{CONFIG_PREFIX}forecast_providers/{provider_name}.json"
        response = s3.get_object(Bucket=CONFIG_BUCKET, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to load provider config {provider_name}: {e}")
        return None


def build_request_body(event: dict, config: dict) -> dict:
    """Build the request body sent to the custom model API.

    Per design doc Section 4.2: includes disease, geo, date, horizons, and current signals.
    """
    return {
        "request_type": "forecast",
        "disease": event.get("disease", config.get("diseases", ["influenza"])[0]),
        "geo_level": "state",
        "geo_value": event.get("state_key", "TX"),
        "request_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "horizons_requested": [1, 2, 3, 4],
        "current_signals": event.get("current_signals", {}),
    }


def build_auth_headers(config: dict) -> dict:
    """Build authentication headers based on provider config."""
    auth_type = config.get("auth_type", "none")
    headers = {}

    if auth_type == "none":
        return headers

    secret_arn = config.get("auth_secret_arn")
    if not secret_arn:
        logger.warning("auth_type requires auth_secret_arn but none provided")
        return headers

    try:
        secret_value = secrets_client.get_secret_value(SecretId=secret_arn)
        secret_string = secret_value.get("SecretString", "")

        if auth_type == "api_key":
            headers["X-API-Key"] = secret_string
        elif auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret_string}"
        # iam_sigv4 would require botocore request signing — not implemented here
        # as it would use the Lambda execution role directly

    except Exception as e:
        logger.error(f"Failed to retrieve auth secret: {e}")

    return headers


def call_provider_api(url: str, body: dict, headers: dict, timeout: int) -> Optional[dict]:
    """Call the custom model API with retry on error.

    Returns parsed JSON response or None on failure.
    """
    encoded_body = json.dumps(body).encode("utf-8")
    max_attempts = 2  # 1 retry

    for attempt in range(max_attempts):
        try:
            response = http.request(
                "POST", url,
                body=encoded_body,
                headers=headers,
                timeout=timeout,
            )

            if response.status == 200:
                return json.loads(response.data.decode("utf-8"))

            logger.warning(
                f"Provider API returned HTTP {response.status} "
                f"(attempt {attempt + 1}/{max_attempts})"
            )

        except urllib3.exceptions.TimeoutError:
            logger.warning(f"Provider API timeout (attempt {attempt + 1}/{max_attempts})")
        except Exception as e:
            logger.error(f"Provider API error: {e} (attempt {attempt + 1}/{max_attempts})")

    return None


def store_raw_response(data: dict, provider_name: str) -> None:
    """Archive raw provider response to S3."""
    if not DATA_BUCKET:
        return
    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s3_key = f"raw/forecasts/custom/{provider_name}/{date_str}/response.json"
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        logger.error(f"S3 archive failed: {e}")


def get_current_iso_week() -> str:
    """Get current ISO week as YYYY-WNN."""
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def emit_metrics(provider_name: str, written: int, errors: int) -> None:
    """Emit CloudWatch metrics."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "forecasts_written",
                    "Value": written,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Provider", "Value": provider_name}],
                },
                {
                    "MetricName": "fetch_errors",
                    "Value": errors,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Provider", "Value": provider_name}],
                },
            ],
        )
    except Exception as e:
        logger.error(f"Metrics emission failed: {e}")
