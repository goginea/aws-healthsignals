"""CDC NWSS Wastewater Fetcher — Config-driven ingestion from data.cdc.gov Socrata API.

Reads Socrata dataset IDs from disease configs and metro county FIPS from state configs.
No hardcoded values — add diseases/states via config files only.
"""
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import boto3
import urllib3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_data_source_config,
    list_active_states,
    list_active_diseases,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

http = urllib3.PoolManager()
s3 = boto3.client("s3")


def lambda_handler(event: dict, context: Any) -> dict:
    """Fetch CDC NWSS wastewater data for all active diseases and states.

    Dynamically reads:
    - Socrata dataset IDs → from disease configs (data_sources.cdc_wastewater)
    - State abbreviations → from state configs (state_abbreviation)
    - Metro county FIPS → from state configs (sentinel_metros.*.county_fips)
    - API settings → from data_sources/cdc_wastewater.json
    """
    system = get_system_config()
    ww_config = get_data_source_config("cdc_wastewater")
    active_states = list_active_states()
    active_diseases = list_active_diseases()

    data_bucket = os.environ.get("DATA_BUCKET", system["infrastructure"]["data_bucket_name_pattern"])
    app_token = os.environ.get(ww_config["api"].get("app_token_env_var", ""), "")

    api_base = ww_config["api"]["base_url"]
    timeout = ww_config["api"]["timeout_seconds"]
    pagination_limit = ww_config["api"]["pagination_limit"]
    max_records = ww_config["api"]["max_records"]
    lookback_days = ww_config["query_defaults"]["lookback_days"]
    state_field = ww_config["query_defaults"]["state_field"]
    date_field = ww_config["query_defaults"]["date_field"]
    fips_field = ww_config["query_defaults"]["county_fips_field"]

    today = datetime.utcnow()
    lookback_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    results = {"fetched": [], "errors": []}

    for disease in active_diseases:
        ww_source = disease.get("data_sources", {}).get("cdc_wastewater")
        if not ww_source or not ww_source.get("socrata_dataset_id"):
            logger.info(f"Skipping {disease['disease_key']} — no wastewater dataset configured")
            continue

        dataset_id = ww_source["socrata_dataset_id"]

        for state in active_states:
            state_abbrev = state["state_abbreviation"]

            # Collect all metro county FIPS for this state
            all_metro_fips = []
            metro_fips_map = {}
            for msa_code, metro_info in state.get("sentinel_metros", {}).items():
                county_fips = metro_info.get("county_fips", [])
                all_metro_fips.extend(county_fips)
                for fips in county_fips:
                    metro_fips_map[fips] = metro_info.get("short_name", msa_code)

            try:
                records = fetch_wastewater_data(
                    api_base=api_base,
                    dataset_id=dataset_id,
                    state_abbrev=state_abbrev,
                    date_after=lookback_date,
                    state_field=state_field,
                    date_field=date_field,
                    app_token=app_token,
                    timeout=timeout,
                    pagination_limit=pagination_limit,
                    max_records=max_records,
                )

                # Filter to metro counties
                metro_records = filter_to_metro_counties(
                    records, all_metro_fips, metro_fips_map, fips_field
                )

                # Store to S3
                s3_key = ww_config["s3_storage"]["prefix_pattern"].format(
                    disease=disease["disease_key"],
                    year=today.strftime("%Y"),
                    week=today.strftime("%W"),
                )
                store_to_s3(
                    {"records": metro_records, "total_unfiltered": len(records)},
                    s3_key, data_bucket,
                )

                results["fetched"].append({
                    "disease": disease["disease_key"],
                    "state": state_abbrev,
                    "dataset_id": dataset_id,
                    "total_records": len(records),
                    "metro_records": len(metro_records),
                })

            except Exception as e:
                error_msg = f"Failed {disease['disease_key']}/{state_abbrev}: {str(e)}"
                results["errors"].append(error_msg)
                logger.error(error_msg)

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results),
    }


def fetch_wastewater_data(
    api_base: str,
    dataset_id: str,
    state_abbrev: str,
    date_after: str,
    state_field: str,
    date_field: str,
    app_token: str = "",
    timeout: int = 30,
    pagination_limit: int = 10000,
    max_records: int = 100000,
) -> list:
    """Query the CDC Socrata SODA API with pagination."""
    all_records = []
    offset = 0

    while True:
        params = {
            "$where": f"{state_field}='{state_abbrev}' AND {date_field} > '{date_after}'",
            "$limit": str(pagination_limit),
            "$offset": str(offset),
            "$order": f"{date_field} DESC",
        }
        url = f"{api_base}/{dataset_id}.json?{urlencode(params)}"
        headers = {"Accept": "application/json"}
        if app_token:
            headers["X-App-Token"] = app_token

        response = http.request("GET", url, headers=headers, timeout=float(timeout))

        if response.status == 429:
            raise RuntimeError("Socrata rate limit exceeded.")
        if response.status != 200:
            raise RuntimeError(f"CDC API returned {response.status}: {response.data.decode()[:500]}")

        batch = json.loads(response.data.decode())
        if not batch:
            break

        all_records.extend(batch)
        offset += pagination_limit

        if offset >= max_records:
            logger.warning(f"Hit max records ({max_records}) for {dataset_id}")
            break

    return all_records


def filter_to_metro_counties(
    records: list,
    metro_fips: list[str],
    metro_fips_map: dict[str, str],
    fips_field: str,
) -> list:
    """Filter records to those serving sentinel metro counties."""
    metro_records = []
    for record in records:
        county_fips_str = record.get(fips_field, "")
        if not county_fips_str:
            continue
        record_fips = [f.strip() for f in str(county_fips_str).split(",")]
        matched = [f for f in record_fips if f in metro_fips]
        if matched:
            record["_matched_metro"] = metro_fips_map.get(matched[0], "unknown")
            metro_records.append(record)
    return metro_records


def store_to_s3(data: dict, key: str, bucket: str) -> None:
    """Store fetched data to S3."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType="application/json",
        Metadata={"source": "cdc-nwss-socrata", "fetched_at": datetime.utcnow().isoformat()},
    )
