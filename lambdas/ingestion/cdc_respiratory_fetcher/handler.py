"""CDC NSSP Respiratory Activity Fetcher — Config-driven ingestion from data.cdc.gov.

Reads state names from state configs and pathogen names from disease configs.
No hardcoded values — add states/diseases via config files only.
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
    """Fetch CDC NSSP ED visit data for all active states and diseases.

    Dynamically reads:
    - State geography names → from state configs (cdc_geography_name)
    - Pathogen names → from disease configs (data_sources.cdc_nssp.pathogen_name)
    - API settings → from data_sources/cdc_nssp.json
    """
    system = get_system_config()
    nssp_config = get_data_source_config("cdc_nssp")
    active_states = list_active_states()
    active_diseases = list_active_diseases()

    data_bucket = os.environ.get("DATA_BUCKET", system["infrastructure"]["data_bucket_name_pattern"])
    app_token = os.environ.get(nssp_config["api"].get("app_token_env_var", ""), "")

    api_base = nssp_config["api"]["base_url"]
    dataset_id = nssp_config["api"]["dataset_id"]
    timeout = nssp_config["api"]["timeout_seconds"]
    max_records = nssp_config["api"]["max_records_per_query"]
    lookback_days = nssp_config["query_defaults"]["lookback_days"]
    visit_type = nssp_config["query_defaults"]["visit_type_filter"]
    always_include = nssp_config["query_defaults"]["always_include_geographies"]

    today = datetime.utcnow()
    lookback_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Build list of geographies to query (states + always-include like "National")
    geographies = list(always_include)
    for state in active_states:
        geo_name = state.get("cdc_geography_name", state["state_name"])
        if geo_name not in geographies:
            geographies.append(geo_name)

    # Build list of pathogens from disease configs
    pathogens = []
    for disease in active_diseases:
        nssp_source = disease.get("data_sources", {}).get("cdc_nssp")
        if nssp_source and nssp_source.get("pathogen_name"):
            pathogens.append({
                "disease_key": disease["disease_key"],
                "pathogen_name": nssp_source["pathogen_name"],
            })

    results = {"fetched": [], "errors": []}

    for geo in geographies:
        for pathogen_info in pathogens:
            pathogen = pathogen_info["pathogen_name"]
            try:
                records = fetch_nssp_data(
                    api_base=api_base,
                    dataset_id=dataset_id,
                    geography=geo,
                    pathogen=pathogen,
                    visit_type=visit_type,
                    date_after=lookback_date,
                    app_token=app_token,
                    timeout=timeout,
                    max_records=max_records,
                )

                results["fetched"].append({
                    "geography": geo,
                    "pathogen": pathogen,
                    "disease_key": pathogen_info["disease_key"],
                    "records": len(records),
                    "latest_week": records[0].get("week_end", "N/A") if records else "N/A",
                    "latest_percent": records[0].get("percent", "N/A") if records else "N/A",
                })

            except Exception as e:
                error_msg = f"Failed NSSP {pathogen}/{geo}: {str(e)}"
                results["errors"].append(error_msg)
                logger.error(error_msg)

    # Store to S3
    if results["fetched"]:
        try:
            s3_key = nssp_config["s3_storage"]["prefix_pattern"].format(
                year=today.strftime("%Y"),
                week=today.strftime("%W"),
            )
            store_to_s3(results, s3_key, data_bucket)
            results["s3_key"] = s3_key
        except Exception as e:
            results["errors"].append(f"S3 storage failed: {str(e)}")

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results),
    }


def fetch_nssp_data(
    api_base: str,
    dataset_id: str,
    geography: str,
    pathogen: str,
    visit_type: str,
    date_after: str,
    app_token: str = "",
    timeout: int = 30,
    max_records: int = 1000,
) -> list:
    """Query CDC NSSP Socrata dataset."""
    where_clauses = [
        f"geography='{geography}'",
        f"pathogen='{pathogen}'",
        f"visit_type='{visit_type}'",
        f"week_end > '{date_after}'",
    ]
    params = {
        "$where": " AND ".join(where_clauses),
        "$order": "week_end DESC",
        "$limit": str(max_records),
    }
    url = f"{api_base}/{dataset_id}.json?{urlencode(params)}"
    headers = {"Accept": "application/json"}
    if app_token:
        headers["X-App-Token"] = app_token

    response = http.request("GET", url, headers=headers, timeout=float(timeout))

    if response.status == 429:
        raise RuntimeError("Socrata rate limit exceeded.")
    if response.status != 200:
        raise RuntimeError(f"CDC NSSP API returned {response.status}: {response.data.decode()[:500]}")

    return json.loads(response.data.decode())


def store_to_s3(data: dict, key: str, bucket: str) -> None:
    """Store fetched data to S3."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2),
        ContentType="application/json",
        Metadata={"source": "cdc-nssp-socrata", "fetched_at": datetime.utcnow().isoformat()},
    )
