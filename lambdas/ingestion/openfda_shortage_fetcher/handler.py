"""OpenFDA Drug Shortages API Fetcher — Config-driven ingestion of pharmaceutical shortage data.

Polls the openFDA Drug Shortages API weekly and stores normalized responses to S3.
Follows the same patterns as delphi_fetcher: SQS trigger, config_loader, urllib3, boto3.

Data Source: https://api.fda.gov/drug/shortages.json
"""
import json
import os
import sys
import logging
import time
from datetime import datetime
from typing import Any

import boto3
import urllib3

# Add shared module to path (for local development — in Lambda, the Layer handles this)
_shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "shared")
_lambdas_path = os.path.join(os.path.dirname(__file__), "..", "..")
if os.path.exists(_shared_path):
    sys.path.insert(0, _shared_path)
    sys.path.insert(0, _lambdas_path)

from shared.config_loader import get_data_source_config, get_system_config
from parser import parse_openfda_response

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")

# Metric constants
METRIC_NAMESPACE = "HealthSignals/DrugShortages"
FUNCTION_NAME = "openfda_shortage_fetcher"


def load_therapeutic_config() -> dict:
    """Load therapeutic categories config using the shared config_loader pattern.

    Uses the config_loader's internal _load_config to load from S3 or local filesystem.
    """
    from shared.config_loader import _load_config
    return _load_config("shortage_monitoring/therapeutic_categories.json")


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch drug shortage data from openFDA API and store to S3.

    Triggered by: SQS message from EventBridge scheduler (weekly Monday 6 AM UTC)

    Implements:
    - Pagination (limit=1000, skip through all results)
    - Retry with exponential backoff for HTTP 429/500/503
    - Skip retry for HTTP 404 (endpoint may have changed)
    - After 3 failed retries, raise exception (SQS will retry, then DLQ)
    - Stores normalized data to S3
    - Emits CloudWatch metrics

    Returns:
        dict with statusCode, s3_key, and records_fetched count.
    """
    log_structured("info", "handler_start", {"event_source": "sqs", "trigger": "weekly_monday"})

    # Parse SQS message from EventBridge trigger
    sqs_records = event.get("Records", [])
    if sqs_records:
        # Extract the message body from the first SQS record
        message_body = json.loads(sqs_records[0].get("body", "{}"))
        log_structured("info", "sqs_message_parsed", {"message_body": message_body})
    else:
        log_structured("info", "direct_invocation", {"event": event})

    # Load configuration
    config = get_data_source_config("openfda_shortages")
    system = get_system_config()

    api_base_url = config["api"]["base_url"]
    timeout = config["api"]["timeout_seconds"]
    max_retries = config["api"]["retry_max_attempts"]
    backoff_base = config["api"]["retry_backoff_seconds"]
    page_limit = config["api"]["pagination"]["default_limit"]

    # Determine data bucket
    data_bucket = os.environ.get(
        "DATA_BUCKET",
        system["infrastructure"]["data_bucket_name_pattern"]
    )

    # Fetch ALL records with pagination
    all_results = []
    skip = 0
    api_success = True

    try:
        while True:
            url = f"{api_base_url}?limit={page_limit}&skip={skip}"
            log_structured("info", "openfda_api_request", {
                "url": url,
                "skip": skip,
                "limit": page_limit,
            })

            response_data = fetch_with_retry(
                url=url,
                timeout=timeout,
                max_retries=max_retries,
                backoff_base=backoff_base,
            )

            results = response_data.get("results", [])
            log_structured("info", "openfda_api_response", {
                "records_in_page": len(results),
                "skip": skip,
            })

            if not results:
                break

            all_results.extend(results)
            skip += page_limit

            # If we got fewer results than the limit, we've reached the end
            if len(results) < page_limit:
                break

    except Exception as e:
        api_success = False
        log_structured("error", "openfda_api_failure", {
            "error": str(e),
            "records_fetched_before_failure": len(all_results),
        })
        emit_metrics(records_fetched=len(all_results), api_success=False)
        raise

    # Normalize the raw response using the parser module
    raw_response = {"results": all_results}
    therapeutic_config = load_therapeutic_config()
    normalized_records = parse_openfda_response(raw_response, therapeutic_config)

    # Store to S3
    s3_key = build_s3_key(config["s3_storage"]["prefix_pattern"])
    store_to_s3(
        data=normalized_records,
        key=s3_key,
        bucket=data_bucket,
    )

    # Emit CloudWatch metrics
    emit_metrics(records_fetched=len(normalized_records), api_success=api_success)

    log_structured("info", "handler_complete", {
        "records_fetched": len(normalized_records),
        "s3_key": s3_key,
    })

    return {
        "statusCode": 200,
        "s3_key": s3_key,
        "records_fetched": len(normalized_records),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def fetch_with_retry(url: str, timeout: int, max_retries: int, backoff_base: int) -> dict:
    """Fetch URL with exponential backoff retry logic.

    Retry for: HTTP 429 (rate limit), 500 (server error), 503 (unavailable)
    Skip retry for: HTTP 404 (endpoint changed — log and exit)
    After max_retries failures: raise exception (SQS will retry, then DLQ)

    Backoff schedule: 5s, 10s, 20s (backoff_base * 2^attempt)
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            response = http.request("GET", url, timeout=float(timeout))

            if response.status == 200:
                return json.loads(response.data.decode())

            if response.status == 404:
                log_structured("error", "openfda_api_404", {
                    "url": url,
                    "message": "Endpoint not found — API may have changed. Skipping retry.",
                })
                raise RuntimeError(
                    f"openFDA API returned 404 for {url}. "
                    "Endpoint may have changed — manual investigation required."
                )

            if response.status in (429, 500, 503):
                wait_time = backoff_base * (2 ** attempt)
                log_structured("warning", "openfda_api_retry", {
                    "status_code": response.status,
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "wait_seconds": wait_time,
                })

                if attempt < max_retries:
                    time.sleep(wait_time)
                    continue
                else:
                    raise RuntimeError(
                        f"openFDA API returned {response.status} after {max_retries} retries. "
                        f"Response: {response.data.decode()[:500]}"
                    )

            # Unexpected status code — no retry
            raise RuntimeError(
                f"openFDA API returned unexpected status {response.status}: "
                f"{response.data.decode()[:500]}"
            )

        except urllib3.exceptions.HTTPError as e:
            last_exception = e
            if attempt < max_retries:
                wait_time = backoff_base * (2 ** attempt)
                log_structured("warning", "openfda_api_network_error", {
                    "error": str(e),
                    "attempt": attempt + 1,
                    "wait_seconds": wait_time,
                })
                time.sleep(wait_time)
            else:
                raise RuntimeError(
                    f"openFDA API network error after {max_retries} retries: {e}"
                ) from last_exception

    # Should not reach here, but just in case
    raise RuntimeError(f"openFDA API fetch failed after {max_retries} retries")


def build_s3_key(pattern: str) -> str:
    """Build time-partitioned S3 key from config pattern.

    Pattern: raw/openfda-shortages/{year}/W{week}/shortages_{timestamp}.json
    """
    now = datetime.utcnow()
    year = now.strftime("%Y")
    week = f"{now.isocalendar()[1]:02d}"
    timestamp = now.strftime("%Y%m%d_%H%M%S")

    return pattern.format(year=year, week=week, timestamp=timestamp)


def store_to_s3(data: list, key: str, bucket: str) -> None:
    """Store normalized shortage records to S3."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType="application/json",
        Metadata={
            "source": "openfda-drug-shortages",
            "fetched_at": datetime.utcnow().isoformat(),
            "records_count": str(len(data)),
        },
    )
    log_structured("info", "s3_put_complete", {"key": key, "records": len(data)})


def emit_metrics(records_fetched: int, api_success: bool) -> None:
    """Emit CloudWatch metrics for monitoring."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "records_fetched_count",
                    "Value": records_fetched,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "FunctionName", "Value": FUNCTION_NAME},
                    ],
                },
                {
                    "MetricName": "api_success_rate",
                    "Value": 1.0 if api_success else 0.0,
                    "Unit": "None",
                    "Dimensions": [
                        {"Name": "FunctionName", "Value": FUNCTION_NAME},
                    ],
                },
            ],
        )
    except Exception as e:
        log_structured("error", "metrics_emission_failed", {"error": str(e)})


def log_structured(level: str, event_type: str, metadata: dict = None) -> None:
    """Emit structured JSON log entry."""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level.upper(),
        "function_name": FUNCTION_NAME,
        "event_type": event_type,
    }
    if metadata:
        log_entry["metadata"] = metadata

    message = json.dumps(log_entry)

    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.info(message)
