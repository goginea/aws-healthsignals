"""FluSight Forecast Fetcher — Ingests CDC FluSight ensemble forecasts from GitHub.

Fetches the latest ensemble CSV from cdcepi/FluSight-forecast-hub on GitHub,
parses quantile predictions, normalizes to the Standard Forecast Contract,
and writes to DynamoDB + S3.

CSV Format (Hubverse):
    reference_date, location, horizon, target, target_end_date, output_type, output_type_id, value

Location codes are 2-digit FIPS state codes (01=AL, 48=TX, US=national).

Environment Variables:
    DATA_BUCKET: S3 bucket for raw archive
    FORECAST_STATE_TABLE: DynamoDB table for normalized forecasts
    CONFIG_BUCKET: S3 bucket for config
    CONFIG_PREFIX: S3 key prefix
    LOG_LEVEL: Logging level
"""
import csv
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
FUNCTION_NAME = "flusight_forecast_fetcher"

forecast_table = dynamodb.Table(FORECAST_STATE_TABLE)

# FIPS state code → 2-letter postal abbreviation (for geo_utils normalization)
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


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch latest FluSight ensemble CSV, parse, normalize, and store.

    Returns:
        {statusCode, forecasts_written, reference_date, errors}
    """
    logger.info(f"FluSight Forecast Fetcher invoked: {json.dumps(event)}")

    # Determine which CSV to fetch (latest by listing repo or using known pattern)
    github_base = "https://raw.githubusercontent.com/cdcepi/FluSight-forecast-hub/main/model-output/FluSight-ensemble"

    # Try to fetch the most recent Wednesday's file
    reference_date = find_latest_reference_date()
    csv_url = f"{github_base}/{reference_date}-FluSight-ensemble.csv"

    logger.info(f"Fetching: {csv_url}")

    # Fetch CSV
    csv_content = fetch_csv(csv_url)
    if not csv_content:
        emit_metrics(0, 1)
        return {"statusCode": 500, "error": "Failed to fetch CSV", "forecasts_written": 0}

    # Store raw CSV to S3
    store_raw_csv(csv_content, reference_date)

    # Parse CSV into Standard Forecast Contract records
    forecasts = parse_flusight_csv(csv_content, reference_date)
    logger.info(f"Parsed {len(forecasts)} forecast records from CSV")

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

    logger.info(f"FluSight fetch complete: {written} written, {errors} errors")
    return {
        "statusCode": 200,
        "forecasts_written": written,
        "reference_date": reference_date,
        "errors": errors,
    }


def find_latest_reference_date() -> str:
    """Find the most recent Wednesday (FluSight publishes on Wednesdays).

    Returns ISO date string (YYYY-MM-DD).
    """
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    # Walk back to most recent Wednesday (weekday 2)
    days_since_wednesday = (today.weekday() - 2) % 7
    latest_wednesday = today - timedelta(days=days_since_wednesday)
    return latest_wednesday.isoformat()


def fetch_csv(url: str) -> str | None:
    """Fetch CSV content from GitHub. Returns None on failure."""
    try:
        response = http.request("GET", url, timeout=30)
        if response.status == 200:
            return response.data.decode("utf-8")
        elif response.status == 404:
            # Try previous week
            logger.warning(f"CSV not found at {url}, trying previous week")
            from datetime import timedelta

            date_str = url.split("/")[-1].split("-FluSight")[0]
            prev_date = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=7)
            prev_url = url.replace(date_str, prev_date.isoformat())
            response = http.request("GET", prev_url, timeout=30)
            if response.status == 200:
                return response.data.decode("utf-8")
            logger.error(f"CSV not found at {prev_url} either")
            return None
        else:
            logger.error(f"GitHub fetch failed: HTTP {response.status}")
            return None
    except Exception as e:
        logger.error(f"CSV fetch error: {e}")
        return None


def parse_flusight_csv(csv_content: str, reference_date: str) -> list[dict]:
    """Parse FluSight Hubverse CSV into Standard Forecast Contract records.

    CSV columns: reference_date, location, horizon, target, target_end_date,
                 output_type, output_type_id, value

    Groups by (location, horizon) and collects quantiles into predictions array.
    """
    reader = csv.DictReader(io.StringIO(csv_content))

    # Group rows by (location, horizon)
    grouped: dict[tuple, dict] = {}

    for row in reader:
        location = row.get("location", "").strip()
        horizon = row.get("horizon", "").strip()
        output_type = row.get("output_type", "").strip()
        output_type_id = row.get("output_type_id", "").strip()
        value = row.get("value", "").strip()

        if not location or not horizon or not value:
            continue

        key = (location, int(horizon))

        if key not in grouped:
            # Convert FIPS code to postal abbreviation
            postal = FIPS_TO_POSTAL.get(location, location)
            geo_level = "national" if location == "US" else "state"

            grouped[key] = {
                "provider": "cdc_flusight",
                "disease": "influenza",
                "geo_level": geo_level,
                "geo_value": postal,
                "forecast_date": reference_date,
                "target": "hospitalizations",
                "trust_weight": 1.0,
                "horizon_weeks": int(horizon),
                "quantiles": {},
                "point_estimate": None,
                "metadata": {
                    "model_count": 32,
                    "ensemble_method": "linear_pool",
                    "source": "cdcepi/FluSight-forecast-hub",
                },
            }

        try:
            val = float(value)
        except ValueError:
            continue

        if output_type == "quantile":
            grouped[key]["quantiles"][output_type_id] = val
            # Use median (0.5) as point estimate
            if output_type_id == "0.5":
                grouped[key]["point_estimate"] = val
        elif output_type == "median":
            grouped[key]["point_estimate"] = val

    # Convert grouped data to Standard Forecast Contract format
    # Group by location → collect all horizons into predictions array
    by_location: dict[str, list] = {}

    for (location, horizon), data in grouped.items():
        if data["point_estimate"] is None:
            # Use 0.5 quantile or skip
            data["point_estimate"] = data["quantiles"].get("0.5", 0)

        if location not in by_location:
            by_location[location] = []

        by_location[location].append(data)

    # Build final forecast records (one per location)
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
            # Include standard quantiles if available
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


def store_raw_csv(csv_content: str, reference_date: str) -> None:
    """Archive raw CSV to S3."""
    if not DATA_BUCKET:
        return
    try:
        s3_key = f"raw/forecasts/flusight/{reference_date}/ensemble.csv"
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key=s3_key,
            Body=csv_content.encode("utf-8"),
            ContentType="text/csv",
        )
        logger.info(f"Archived CSV: s3://{DATA_BUCKET}/{s3_key}")
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
                    "Dimensions": [{"Name": "Provider", "Value": "cdc_flusight"}],
                },
                {
                    "MetricName": "fetch_errors",
                    "Value": errors,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Provider", "Value": "cdc_flusight"}],
                },
            ],
        )
    except Exception as e:
        logger.error(f"Metrics emission failed: {e}")
