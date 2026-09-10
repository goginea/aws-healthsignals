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
    - Socrata dataset ID + pathogen_target → from disease configs (data_sources.cdc_wastewater)
    - Full state names → from state configs (state_name)
    - Metro county names → from state configs (sentinel_metros.*.county_names)
    - API settings → from data_sources/cdc_wastewater.json

    Uses the unified CDC dataset atcp-73re (viral activity level for
    SARS-CoV-2, Influenza A, and RSV), keyed by full state name and pathogen.
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
    pathogen_field = ww_config["query_defaults"]["pathogen_field"]
    county_names_field = ww_config["query_defaults"]["county_names_field"]

    today = datetime.utcnow()
    lookback_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    results = {"fetched": [], "errors": []}

    for disease in active_diseases:
        ww_source = disease.get("data_sources", {}).get("cdc_wastewater")
        if not ww_source or not ww_source.get("socrata_dataset_id"):
            logger.info(f"Skipping {disease['disease_key']} — no wastewater dataset configured")
            continue

        dataset_id = ww_source["socrata_dataset_id"]
        pathogen_target = ww_source.get("pathogen_target")
        if not pathogen_target:
            logger.info(f"Skipping {disease['disease_key']} — no pathogen_target configured")
            continue

        for state in active_states:
            # atcp-73re keys state by full name (e.g. "Texas"), not abbreviation.
            state_name = state.get("state_name", state.get("state_abbreviation"))

            # Collect all metro county NAMES for this state (dataset exposes
            # counties_served by name, not FIPS).
            metro_name_map = {}
            for msa_code, metro_info in state.get("sentinel_metros", {}).items():
                for cname in metro_info.get("county_names", []):
                    metro_name_map[cname.strip().lower()] = metro_info.get("short_name", msa_code)

            try:
                records = fetch_wastewater_data(
                    api_base=api_base,
                    dataset_id=dataset_id,
                    state_value=state_name,
                    pathogen_target=pathogen_target,
                    date_after=lookback_date,
                    state_field=state_field,
                    pathogen_field=pathogen_field,
                    date_field=date_field,
                    app_token=app_token,
                    timeout=timeout,
                    pagination_limit=pagination_limit,
                    max_records=max_records,
                )

                # Filter to metro counties by county name
                metro_records = filter_to_metro_counties(
                    records, metro_name_map, county_names_field
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
                    "state": state_name,
                    "dataset_id": dataset_id,
                    "total_records": len(records),
                    "metro_records": len(metro_records),
                })

            except Exception as e:
                error_msg = f"Failed {disease['disease_key']}/{state.get('state_abbreviation')}: {str(e)}"
                results["errors"].append(error_msg)
                logger.error(error_msg)

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results),
    }


def fetch_wastewater_data(
    api_base: str,
    dataset_id: str,
    state_value: str,
    pathogen_target: str,
    date_after: str,
    state_field: str,
    pathogen_field: str,
    date_field: str,
    app_token: str = "",
    timeout: int = 30,
    pagination_limit: int = 10000,
    max_records: int = 100000,
) -> list:
    """Query the CDC Socrata SODA API (atcp-73re) with pagination.

    Filters by full state name, pathogen_target, and a date lower bound.
    """
    all_records = []
    offset = 0

    # Escape single quotes in string literals for SoQL safety.
    state_lit = state_value.replace("'", "''")
    pathogen_lit = pathogen_target.replace("'", "''")

    while True:
        params = {
            "$where": (
                f"{state_field}='{state_lit}' AND "
                f"{pathogen_field}='{pathogen_lit}' AND "
                f"{date_field} > '{date_after}'"
            ),
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
    metro_name_map: dict[str, str],
    county_names_field: str,
) -> list:
    """Filter records to those serving sentinel metro counties, by county name.

    The atcp-73re dataset reports counties by name in ``counties_served``
    (a single name or a comma-separated list), not by FIPS. Match case-
    insensitively against the configured metro county names.
    """
    metro_records = []
    for record in records:
        counties_str = record.get(county_names_field, "")
        if not counties_str:
            continue
        record_counties = [c.strip().lower() for c in str(counties_str).split(",")]
        matched = [c for c in record_counties if c in metro_name_map]
        if matched:
            record["_matched_metro"] = metro_name_map.get(matched[0], "unknown")
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
