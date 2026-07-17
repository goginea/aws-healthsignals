"""RSV Hub Forecast Fetcher — Ingests RSV ensemble forecasts from HopkinsIDD/rsv-forecast-hub.

Fetches the latest ensemble parquet file from GitHub, parses quantile predictions
using pandas + pyarrow, normalizes to the Standard Forecast Contract, and writes
to DynamoDB + S3.

The RSV Hub uses Hubverse format (same columns as FluSight) but ships as parquet:
    reference_date, location, horizon, target, target_end_date, output_type, output_type_id, value

Location codes are 2-digit FIPS state codes.

Environment Variables:
    DATA_BUCKET: S3 bucket for raw archive
    FORECAST_STATE_TABLE: DynamoDB table for normalized forecasts
    CONFIG_BUCKET: S3 bucket for config
    CONFIG_PREFIX: S3 key prefix
    LOG_LEVEL: Logging level
"""
import io
import json
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

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

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
FORECAST_STATE_TABLE = os.environ.get("FORECAST_STATE_TABLE", "healthsignals-forecast-state")
METRIC_NAMESPACE = "HealthSignals/ForecastProviders"

forecast_table = dynamodb.Table(FORECAST_STATE_TABLE)

# Same FIPS mapping as FluSight fetcher
FIPS_TO_POSTAL = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "US": "US",
}

# GitHub API for listing files in the ensemble directory
GITHUB_API_URL = "https://api.github.com/repos/HopkinsIDD/rsv-forecast-hub/contents/model-output/hub-ensemble"


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch latest RSV Hub ensemble parquet, parse, normalize, and store."""
    logger.info(f"RSV Hub Forecast Fetcher invoked: {json.dumps(event)}")

    # Find the latest parquet file via GitHub API
    latest_file = find_latest_parquet()
    if not latest_file:
        emit_metrics(0, 1)
        return {"statusCode": 500, "error": "No parquet file found", "forecasts_written": 0}

    file_name = latest_file["name"]
    download_url = latest_file["download_url"]
    reference_date = file_name.split("-hub-ensemble")[0]

    logger.info(f"Fetching: {file_name} ({download_url})")

    # Download parquet file
    parquet_bytes = fetch_parquet(download_url)
    if not parquet_bytes:
        emit_metrics(0, 1)
        return {"statusCode": 500, "error": "Failed to download parquet", "forecasts_written": 0}

    # Store raw parquet to S3
    store_raw_parquet(parquet_bytes, reference_date)

    # Parse parquet into Standard Forecast Contract records
    forecasts = parse_rsv_parquet(parquet_bytes, reference_date)
    logger.info(f"Parsed {len(forecasts)} forecast records from parquet")

    # Validate, normalize, and write to DynamoDB
    written = 0
    errors = 0
    week = date_to_iso_week(reference_date)

    for forecast in forecasts:
        if not validate_forecast(forecast):
            errors += 1
            continue

        normalized = normalize_forecast_geo(forecast)
        item = forecast_to_dynamodb_item(normalized, week)

        try:
            forecast_table.put_item(Item=item)
            written += 1
        except Exception as e:
            logger.error(f"DynamoDB write failed: {e}")
            errors += 1

    emit_metrics(written, errors)

    logger.info(f"RSV Hub fetch complete: {written} written, {errors} errors")
    return {
        "statusCode": 200,
        "forecasts_written": written,
        "reference_date": reference_date,
        "errors": errors,
    }


def find_latest_parquet() -> dict | None:
    """Find the most recent parquet file in the RSV Hub GitHub repo."""
    try:
        response = http.request("GET", GITHUB_API_URL, timeout=15, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "HealthSignals-RSV-Fetcher",
        })
        if response.status != 200:
            logger.error(f"GitHub API returned {response.status}")
            return None

        files = json.loads(response.data.decode("utf-8"))
        parquet_files = [f for f in files if f["name"].endswith(".parquet")]

        if not parquet_files:
            return None

        # Sort by name (date-prefixed) to get latest
        parquet_files.sort(key=lambda f: f["name"], reverse=True)
        return parquet_files[0]

    except Exception as e:
        logger.error(f"GitHub API error: {e}")
        return None


def fetch_parquet(url: str) -> bytes | None:
    """Download parquet file from GitHub."""
    try:
        response = http.request("GET", url, timeout=60)
        if response.status == 200:
            return response.data
        logger.error(f"Parquet download failed: HTTP {response.status}")
        return None
    except Exception as e:
        logger.error(f"Parquet fetch error: {e}")
        return None


def parse_rsv_parquet(parquet_bytes: bytes, reference_date: str) -> list[dict]:
    """Parse RSV Hub parquet file into Standard Forecast Contract records.

    Uses pandas + pyarrow to read parquet. Same column structure as FluSight CSV.
    """
    try:
        import pandas as pd

        df = pd.read_parquet(io.BytesIO(parquet_bytes))
    except ImportError:
        logger.error("pandas/pyarrow not available — cannot parse parquet")
        return []
    except Exception as e:
        logger.error(f"Parquet parse error: {e}")
        return []

    # Expected columns: reference_date, location, horizon, target,
    # target_end_date, output_type, output_type_id, value
    required_cols = {"location", "horizon", "output_type", "output_type_id", "value"}
    if not required_cols.issubset(set(df.columns)):
        logger.error(f"Missing columns. Found: {list(df.columns)}")
        return []

    # Group by (location, horizon) — same logic as FluSight
    grouped: dict[tuple, dict] = {}

    for _, row in df.iterrows():
        location = str(row.get("location", "")).strip()
        try:
            horizon = int(row.get("horizon", 0))
        except (ValueError, TypeError):
            continue
        output_type = str(row.get("output_type", "")).strip()
        output_type_id = str(row.get("output_type_id", "")).strip()
        try:
            value = float(row.get("value", 0))
        except (ValueError, TypeError):
            continue

        if not location:
            continue

        key = (location, horizon)

        if key not in grouped:
            postal = FIPS_TO_POSTAL.get(location, location)
            geo_level = "national" if location == "US" else "state"

            grouped[key] = {
                "provider": "cdc_rsv_hub",
                "disease": "rsv",
                "geo_level": geo_level,
                "geo_value": postal,
                "forecast_date": reference_date,
                "target": "hospitalizations",
                "trust_weight": 1.0,
                "horizon_weeks": horizon,
                "quantiles": {},
                "point_estimate": None,
                "metadata": {
                    "ensemble_method": "linear_pool",
                    "source": "HopkinsIDD/rsv-forecast-hub",
                },
            }

        if output_type == "quantile":
            grouped[key]["quantiles"][output_type_id] = value
            if output_type_id == "0.5":
                grouped[key]["point_estimate"] = value
        elif output_type == "median":
            grouped[key]["point_estimate"] = value

    # Build forecast records (one per location with predictions array)
    by_location: dict[str, list] = {}
    for (location, horizon), data in grouped.items():
        if data["point_estimate"] is None:
            data["point_estimate"] = data["quantiles"].get("0.5", 0)
        if location not in by_location:
            by_location[location] = []
        by_location[location].append(data)

    forecasts = []
    for location, horizons in by_location.items():
        if not horizons:
            continue
        first = horizons[0]
        predictions = []
        for h in sorted(horizons, key=lambda x: x["horizon_weeks"]):
            pred = {
                "horizon_weeks": h["horizon_weeks"],
                "point_estimate": h["point_estimate"],
            }
            quantiles = {}
            for q_key in ["0.025", "0.25", "0.5", "0.75", "0.975"]:
                if q_key in h["quantiles"]:
                    quantiles[q_key] = h["quantiles"][q_key]
            if quantiles:
                pred["quantiles"] = quantiles
            predictions.append(pred)

        forecast = {
            "provider": first["provider"],
            "disease": first["disease"],
            "geo_level": first["geo_level"],
            "geo_value": first["geo_value"],
            "forecast_date": first["forecast_date"],
            "target": first["target"],
            "trust_weight": first["trust_weight"],
            "predictions": predictions,
            "metadata": first["metadata"],
        }
        forecasts.append(forecast)

    return forecasts


def store_raw_parquet(parquet_bytes: bytes, reference_date: str) -> None:
    """Archive raw parquet to S3."""
    if not DATA_BUCKET:
        return
    try:
        s3_key = f"raw/forecasts/rsv_hub/{reference_date}/ensemble.parquet"
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key=s3_key,
            Body=parquet_bytes,
            ContentType="application/octet-stream",
        )
        logger.info(f"Archived parquet: s3://{DATA_BUCKET}/{s3_key}")
    except Exception as e:
        logger.error(f"S3 archive failed: {e}")


def date_to_iso_week(date_str: str) -> str:
    """Convert YYYY-MM-DD to ISO week format YYYY-WNN."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def emit_metrics(written: int, errors: int) -> None:
    """Emit CloudWatch metrics."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "forecasts_written",
                    "Value": written,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Provider", "Value": "cdc_rsv_hub"}],
                },
                {
                    "MetricName": "fetch_errors",
                    "Value": errors,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Provider", "Value": "cdc_rsv_hub"}],
                },
            ],
        )
    except Exception as e:
        logger.error(f"Metrics emission failed: {e}")
