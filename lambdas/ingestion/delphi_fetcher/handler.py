"""Delphi Epidata API Fetcher — Config-driven ingestion of syndromic surveillance data.

Reads sentinel metros from state configs and signals from disease configs.
No hardcoded values — add states/diseases via config files only.

Data Source: https://api.delphi.cmu.edu/epidata/covidcast/
"""
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Any

import boto3
import urllib3

# Add shared module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_data_source_config,
    get_all_sentinel_metros,
    list_active_diseases,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch surveillance data from CMU Delphi Epidata API for all active states/diseases.

    Dynamically reads:
    - Which metros to monitor → from state configs (sentinel_metros)
    - Which signals to fetch → from disease configs (data_sources.delphi)
    - API settings → from data_sources/delphi.json

    Returns:
        dict with statusCode, signals_fetched count, and any errors.
    """
    # Load configs
    system = get_system_config()
    delphi_config = get_data_source_config("delphi")
    all_metros = get_all_sentinel_metros()
    active_diseases = list_active_diseases()

    # Determine data bucket
    data_bucket = os.environ.get(
        "DATA_BUCKET",
        system["infrastructure"]["data_bucket_name_pattern"]
    )

    # API settings from config
    api_base = delphi_config["api"]["base_url"]
    timeout = delphi_config["api"]["timeout_seconds"]
    lookback_weeks = delphi_config["query_defaults"]["lookback_weeks"]
    geo_type = delphi_config["query_defaults"]["geo_type"]

    # Calculate date range
    today = datetime.utcnow()
    start_date = (today - timedelta(weeks=lookback_weeks)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    results = {"fetched": [], "errors": []}

    # For each active disease, get the Delphi signal
    for disease in active_diseases:
        delphi_source = disease.get("data_sources", {}).get("delphi")
        if not delphi_source or not delphi_source.get("data_source"):
            logger.info(f"Skipping {disease['disease_key']} — no Delphi signal configured")
            continue

        data_source = delphi_source["data_source"]
        signal_name = delphi_source["signal"]

        # For each sentinel metro across all active states
        for msa_code, metro_info in all_metros.items():
            try:
                data = fetch_signal(
                    api_base=api_base,
                    data_source=data_source,
                    signal_name=signal_name,
                    geo_type=geo_type,
                    geo_value=msa_code,
                    start_date=start_date,
                    end_date=end_date,
                    timeout=timeout,
                )

                s3_key = build_s3_key(
                    today, data_source, signal_name, msa_code,
                    delphi_config["s3_storage"]["prefix_pattern"]
                )
                store_to_s3(data, s3_key, data_bucket)

                record_count = len(data.get("epidata", []))
                results["fetched"].append({
                    "disease": disease["disease_key"],
                    "signal": f"{data_source}:{signal_name}",
                    "metro": metro_info.get("short_name", msa_code),
                    "state": metro_info.get("state_abbreviation", "??"),
                    "records": record_count,
                })
                logger.info(
                    f"Fetched {data_source}:{signal_name} for "
                    f"{metro_info.get('short_name', msa_code)}: {record_count} records"
                )

            except Exception as e:
                error_msg = (
                    f"Failed {data_source}:{signal_name} / "
                    f"{metro_info.get('short_name', msa_code)}: {str(e)}"
                )
                results["errors"].append(error_msg)
                logger.error(error_msg)

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results),
    }


def fetch_signal(
    api_base: str,
    data_source: str,
    signal_name: str,
    geo_type: str,
    geo_value: str,
    start_date: str,
    end_date: str,
    timeout: int = 30,
) -> dict:
    """Call the Delphi Epidata covidcast endpoint."""
    params = {
        "data_source": data_source,
        "signal": signal_name,
        "geo_type": geo_type,
        "geo_value": geo_value,
        "time_type": "day",
        "time_values": f"{start_date}-{end_date}",
    }

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{api_base}?{query_string}"

    logger.info(f"Fetching: {url}")
    response = http.request("GET", url, timeout=float(timeout))

    if response.status != 200:
        raise RuntimeError(f"Delphi API returned {response.status}: {response.data.decode()[:500]}")

    return json.loads(response.data.decode())


def build_s3_key(
    fetch_date: datetime,
    data_source: str,
    signal_name: str,
    msa_code: str,
    pattern: str,
) -> str:
    """Build time-partitioned S3 key from config pattern."""
    year = fetch_date.strftime("%Y")
    week = fetch_date.strftime("%W")
    return pattern.format(
        data_source=data_source,
        signal=signal_name,
        year=year,
        week=week,
        geo_value=msa_code,
    )


def store_to_s3(data: dict, key: str, bucket: str) -> None:
    """Store raw API response to S3."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType="application/json",
        Metadata={
            "source": "cmu-delphi-epidata",
            "fetched_at": datetime.utcnow().isoformat(),
        },
    )
