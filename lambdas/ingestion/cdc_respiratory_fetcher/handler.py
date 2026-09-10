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
    get_all_sentinel_metros,
    get_subscribing_counties,
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
    # Percentage field name in the dataset (vutn-jzwm uses "percent_visits").
    percent_field = nssp_config.get("key_fields", {}).get("percent", "percent_visits")
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
                    "latest_percent": records[0].get(percent_field, "N/A") if records else "N/A",
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

    # --- County-level ingestion (supplemental) ---
    # Additive to the state-level feed above. Pulls county-level ED-visit %s
    # and trajectory (trend) for sentinel-metro counties + subscribing counties
    # from the wide-format dataset, writing one file per county/disease/week.
    # Failures here never affect the state-level result.
    try:
        county_results = fetch_county_level_data(
            active_states=active_states,
            active_diseases=active_diseases,
            data_bucket=data_bucket,
            app_token=app_token,
            today=today,
        )
        results["county"] = county_results
    except Exception as e:
        error_msg = f"County-level ingestion failed: {str(e)}"
        results["errors"].append(error_msg)
        logger.error(error_msg)

    return {
        "statusCode": 200 if not results["errors"] else 207,
        "body": json.dumps(results),
    }


def fetch_nssp_data(
    api_base: str,
    dataset_id: str,
    geography: str,
    pathogen: str,
    date_after: str,
    app_token: str = "",
    timeout: int = 30,
    max_records: int = 1000,
) -> list:
    """Query CDC NSSP Socrata dataset (vutn-jzwm).

    Schema: geography, pathogen, percent_visits, week_end. There is no
    visit_type column (the dataset is ED-visit percentages by construction).
    """
    where_clauses = [
        f"geography='{geography}'",
        f"pathogen='{pathogen}'",
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


# ---------------------------------------------------------------------------
# County-level ingestion (supplemental) — wide-format dataset (rdmq-nq56)
# ---------------------------------------------------------------------------


def _collect_target_counties(active_states: list) -> dict:
    """Build the set of county FIPS codes to fetch, with metadata.

    Includes every sentinel-metro county (so we can corroborate/gap-fill metro
    signals) plus every subscribing county (so each rural county gets its own
    measured signal for alert context).

    Returns:
        dict mapping county FIPS -> {"county_name": str, "state_key": str,
        "roles": set([...])} where roles is any of {"metro", "subscribing"}.
    """
    targets: dict = {}

    def _add(fips: str, name: str, state_key: str, role: str):
        if not fips:
            return
        entry = targets.setdefault(
            fips,
            {"county_name": name or "", "state_key": state_key, "roles": set()},
        )
        entry["roles"].add(role)
        if name and not entry["county_name"]:
            entry["county_name"] = name

    for state in active_states:
        state_key = state.get("state_key", "unknown")

        # Sentinel-metro counties
        for metro_info in state.get("sentinel_metros", {}).values():
            fips_list = metro_info.get("county_fips", [])
            name_list = metro_info.get("county_names", [])
            for idx, fips in enumerate(fips_list):
                name = name_list[idx] if idx < len(name_list) else ""
                _add(fips, name, state_key, "metro")

        # Subscribing counties
        for county in state.get("subscribing_counties", []):
            _add(
                county.get("county_fips", ""),
                county.get("county_name", ""),
                state_key,
                "subscribing",
            )

    return targets


def fetch_county_level_data(
    active_states: list,
    active_diseases: list,
    data_bucket: str,
    app_token: str,
    today: datetime,
) -> dict:
    """Fetch county-level ED-visit %s + trend for all target counties.

    For each target county FIPS we pull the latest wide-format row from
    rdmq-nq56 and derive a per-disease record ({value, trend, ...}) for each
    active disease that has a wide-format column mapping. Each per-county,
    per-disease signal is written to its own S3 key so downstream lookups are
    cheap (mirrors the Delphi per-county layout).

    Returns a summary dict; never raises for a single-county failure.
    """
    county_config = get_data_source_config("cdc_nssp_county")

    if not county_config.get("enabled", True):
        return {"skipped": True, "reason": "cdc_nssp_county disabled in config"}

    api = county_config["api"]
    api_base = api["base_url"]
    dataset_id = api["dataset_id"]
    timeout = api["timeout_seconds"]
    max_records = api["max_records_per_query"]

    qd = county_config["query_defaults"]
    fips_field = qd.get("fips_field", "fips")
    week_end_field = qd.get("week_end_field", "week_end")
    lookback_days = qd.get("lookback_days", 60)

    wide_fields = county_config["wide_format_fields"]
    trend_mapping = county_config["trend_mapping"]
    s3_prefix_pattern = county_config["s3_storage"]["prefix_pattern"]

    lookback_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Only fetch diseases that have a wide-format column mapping.
    disease_keys = [
        d["disease_key"]
        for d in active_diseases
        if d.get("disease_key") in wide_fields
    ]

    targets = _collect_target_counties(active_states)

    summary = {
        "dataset_id": dataset_id,
        "counties_targeted": len(targets),
        "diseases": disease_keys,
        "written": [],
        "no_data": [],
        "errors": [],
    }

    for fips, meta in targets.items():
        try:
            row = fetch_county_row(
                api_base=api_base,
                dataset_id=dataset_id,
                fips=fips,
                fips_field=fips_field,
                week_end_field=week_end_field,
                date_after=lookback_date,
                app_token=app_token,
                timeout=timeout,
                max_records=max_records,
            )
        except Exception as e:
            summary["errors"].append(f"Fetch failed for FIPS {fips}: {str(e)}")
            logger.error(f"County fetch failed for FIPS {fips}: {e}")
            continue

        if not row:
            summary["no_data"].append(fips)
            continue

        for disease_key in disease_keys:
            record = parse_county_row(
                row=row,
                disease_key=disease_key,
                field_map=wide_fields[disease_key],
                trend_mapping=trend_mapping,
                meta=meta,
                fips=fips,
                week_end_field=week_end_field,
            )
            if record is None:
                continue

            week_end = record.get("week_end", "")
            year, week = _year_week_from_week_end(week_end, today)
            s3_key = s3_prefix_pattern.format(
                disease=disease_key,
                year=year,
                week=week,
                fips=fips,
            )
            try:
                store_to_s3(record, s3_key, data_bucket)
                summary["written"].append(s3_key)
            except Exception as e:
                summary["errors"].append(f"S3 write failed {s3_key}: {str(e)}")
                logger.error(f"County S3 write failed for {s3_key}: {e}")

    return summary


def fetch_county_row(
    api_base: str,
    dataset_id: str,
    fips: str,
    fips_field: str,
    week_end_field: str,
    date_after: str,
    app_token: str = "",
    timeout: int = 30,
    max_records: int = 1000,
) -> dict:
    """Query the wide-format county dataset for a single FIPS, latest week.

    Returns the most recent row (dict) for the county, or {} if none.
    """
    where_clauses = [
        f"{fips_field}='{fips}'",
        f"{week_end_field} > '{date_after}'",
    ]
    params = {
        "$where": " AND ".join(where_clauses),
        "$order": f"{week_end_field} DESC",
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
        raise RuntimeError(
            f"CDC NSSP county API returned {response.status}: "
            f"{response.data.decode()[:500]}"
        )

    rows = json.loads(response.data.decode())
    return rows[0] if rows else {}


def parse_county_row(
    row: dict,
    disease_key: str,
    field_map: dict,
    trend_mapping: dict,
    meta: dict,
    fips: str,
    week_end_field: str = "week_end",
) -> dict:
    """Convert a wide-format county row into a per-disease signal record.

    Returns None if the disease's percentage value is missing/unparseable so
    we don't persist empty signals (keeps this strictly supplemental — an
    absent county value simply contributes nothing downstream).
    """
    percent_col = field_map.get("percent")
    smoothed_col = field_map.get("percent_smoothed")
    trend_col = field_map.get("trend")

    value = _to_float(row.get(percent_col))
    if value is None:
        return None

    smoothed_value = _to_float(row.get(smoothed_col))
    raw_trend = (row.get(trend_col) or "").strip()
    trend = trend_mapping.get(raw_trend, "unknown")

    return {
        "source": "cdc_nssp_county",
        "dataset_id": "rdmq-nq56",
        "disease": disease_key,
        "fips": fips,
        "county_name": meta.get("county_name", row.get("county", "")),
        "state_key": meta.get("state_key", "unknown"),
        "roles": sorted(meta.get("roles", [])),
        "geography": row.get("geography", ""),
        "week_end": row.get(week_end_field, ""),
        "value": value,
        "smoothed_value": smoothed_value,
        "trend": trend,
        "trend_raw": raw_trend,
        "fetched_at": datetime.utcnow().isoformat(),
    }


def _to_float(raw: Any) -> float:
    """Parse a Socrata string/number to float, returning None if not parseable."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _year_week_from_week_end(week_end: str, fallback_today: datetime) -> tuple:
    """Derive (year, week-number) strings from a week_end ISO date.

    The 'W' prefix in the S3 key comes from the literal in prefix_pattern, so
    this returns just the zero-padded week number (e.g. "35"). Uses the data's
    own week_end so the S3 layout reflects the reporting week rather than the
    fetch date. Falls back to the run date if unparseable.
    """
    try:
        dt = datetime.strptime(week_end[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        dt = fallback_today
    return dt.strftime("%Y"), dt.strftime("%W")
