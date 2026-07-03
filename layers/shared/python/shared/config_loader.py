"""Config Loader — Centralized configuration management for HealthSignals.

Loads config from S3 (production) or local filesystem (development/testing).
Caches all configs in memory for Lambda warm-start reuse.

Environment Variables:
    CONFIG_BUCKET: S3 bucket containing config files (if set, loads from S3)
    CONFIG_PREFIX: S3 key prefix for config files (default: "config/")
    CONFIG_LOCAL_PATH: Local filesystem path to config/ directory (fallback)
    AWS_REGION: AWS region for S3 client

Usage:
    from shared.config_loader import get_system_config, get_state_config, list_active_states

    system = get_system_config()
    texas = get_state_config("texas")
    states = list_active_states()
"""
import json
import os
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- Configuration ---
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")
CONFIG_PREFIX = os.environ.get("CONFIG_PREFIX", "config/")
CONFIG_LOCAL_PATH = os.environ.get(
    "CONFIG_LOCAL_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "config")
)

# --- Cache ---
_cache: dict[str, Any] = {}
_s3_client = None


class ConfigLoadError(Exception):
    """Raised when a required config file cannot be loaded or is invalid."""
    pass


def _get_s3_client():
    """Lazy-initialize S3 client."""
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


def _load_json_from_s3(key: str) -> dict:
    """Load and parse a JSON file from S3."""
    try:
        client = _get_s3_client()
        response = client.get_object(Bucket=CONFIG_BUCKET, Key=key)
        content = response["Body"].read().decode("utf-8")
        return json.loads(content)
    except Exception as e:
        raise ConfigLoadError(f"Failed to load s3://{CONFIG_BUCKET}/{key}: {e}")


def _load_json_from_local(relative_path: str) -> dict:
    """Load and parse a JSON file from local filesystem."""
    full_path = Path(CONFIG_LOCAL_PATH) / relative_path
    if not full_path.exists():
        raise ConfigLoadError(f"Config file not found: {full_path}")
    try:
        with open(full_path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigLoadError(f"Invalid JSON in {full_path}: {e}")


def _load_config(relative_path: str) -> dict:
    """Load config from S3 or local filesystem with caching.

    Strategy:
    1. Check in-memory cache first (Lambda warm start)
    2. If CONFIG_BUCKET is set, load from S3
    3. Otherwise, load from local filesystem (development/testing)
    """
    cache_key = relative_path
    if cache_key in _cache:
        return _cache[cache_key]

    if CONFIG_BUCKET:
        s3_key = f"{CONFIG_PREFIX}{relative_path}"
        data = _load_json_from_s3(s3_key)
    else:
        data = _load_json_from_local(relative_path)

    _cache[cache_key] = data
    return data


def _list_configs_in_directory(directory: str, exclude_prefix: str = "_") -> list[str]:
    """List available config files in a directory (excluding templates)."""
    if CONFIG_BUCKET:
        client = _get_s3_client()
        prefix = f"{CONFIG_PREFIX}{directory}/"
        response = client.list_objects_v2(Bucket=CONFIG_BUCKET, Prefix=prefix)
        files = []
        for obj in response.get("Contents", []):
            filename = obj["Key"].replace(prefix, "")
            if filename.endswith(".json") and not filename.startswith(exclude_prefix):
                files.append(filename.replace(".json", ""))
        return files
    else:
        local_dir = Path(CONFIG_LOCAL_PATH) / directory
        if not local_dir.exists():
            return []
        return [
            f.stem for f in local_dir.glob("*.json")
            if not f.name.startswith(exclude_prefix)
        ]


def invalidate_cache(key: Optional[str] = None):
    """Clear config cache. Call when configs are updated at runtime.

    Args:
        key: Specific cache key to invalidate, or None to clear all.
    """
    global _cache
    if key:
        _cache.pop(key, None)
    else:
        _cache = {}


# === Public API ===


def get_system_config() -> dict:
    """Load the global system configuration.

    Returns:
        dict with infrastructure, DynamoDB table names, Bedrock model IDs, etc.

    Raises:
        ConfigLoadError if system.json is missing or invalid.
    """
    return _load_config("system.json")


def get_state_config(state_key: str) -> dict:
    """Load configuration for a specific state.

    Args:
        state_key: Lowercase state identifier (e.g., "texas", "florida")

    Returns:
        dict with sentinel_metros, subscribing_counties, disease_overrides, etc.

    Raises:
        ConfigLoadError if the state config doesn't exist.
    """
    config = _load_config(f"states/{state_key}.json")
    _validate_state_config(config)
    return config


def get_disease_config(disease_key: str) -> dict:
    """Load configuration for a specific disease.

    Args:
        disease_key: Lowercase disease identifier (e.g., "influenza", "rsv", "covid")

    Returns:
        dict with detection thresholds, data sources, severity classification, etc.

    Raises:
        ConfigLoadError if the disease config doesn't exist.
    """
    config = _load_config(f"diseases/{disease_key}.json")
    _validate_disease_config(config)
    return config


def get_data_source_config(source_name: str) -> dict:
    """Load configuration for a specific data source.

    Args:
        source_name: Data source identifier (e.g., "delphi", "cdc_wastewater", "cdc_nssp")

    Returns:
        dict with API endpoints, auth settings, field mappings, etc.

    Raises:
        ConfigLoadError if the data source config doesn't exist.
    """
    return _load_config(f"data_sources/{source_name}.json")


def list_active_states() -> list[dict]:
    """List all enabled state configurations.

    Returns:
        List of state config dicts where enabled=True.
    """
    state_keys = _list_configs_in_directory("states")
    active = []
    for key in state_keys:
        try:
            config = get_state_config(key)
            if config.get("enabled", False):
                active.append(config)
        except ConfigLoadError as e:
            logger.warning(f"Skipping invalid state config '{key}': {e}")
    return active


def list_active_diseases() -> list[dict]:
    """List all enabled disease configurations.

    Returns:
        List of disease config dicts where enabled=True.
    """
    disease_keys = _list_configs_in_directory("diseases")
    active = []
    for key in disease_keys:
        try:
            config = get_disease_config(key)
            if config.get("enabled", False):
                active.append(config)
        except ConfigLoadError as e:
            logger.warning(f"Skipping invalid disease config '{key}': {e}")
    return active


def get_all_sentinel_metros() -> dict[str, dict]:
    """Aggregate all sentinel metros across all active states.

    Returns:
        dict mapping MSA FIPS code → metro info dict (includes state_key).
    """
    all_metros = {}
    for state in list_active_states():
        for msa_code, metro_info in state.get("sentinel_metros", {}).items():
            all_metros[msa_code] = {
                **metro_info,
                "state_key": state["state_key"],
                "state_abbreviation": state["state_abbreviation"],
            }
    return all_metros


def get_all_metro_county_fips() -> dict[str, list[str]]:
    """Aggregate all metro county FIPS codes across all active states.

    Returns:
        dict mapping metro short_name → list of county FIPS codes.
    """
    result = {}
    for state in list_active_states():
        for msa_code, metro_info in state.get("sentinel_metros", {}).items():
            label = f"{metro_info.get('short_name', msa_code)} ({msa_code})"
            result[label] = metro_info.get("county_fips", [])
    return result


def get_subscribing_counties(state_key: Optional[str] = None) -> list[dict]:
    """Get all subscribing counties, optionally filtered by state.

    Args:
        state_key: If provided, only return counties from this state.

    Returns:
        List of county config dicts with contacts and delivery preferences.
    """
    if state_key:
        state = get_state_config(state_key)
        return state.get("subscribing_counties", [])

    all_counties = []
    for state in list_active_states():
        for county in state.get("subscribing_counties", []):
            county["_state_key"] = state["state_key"]
            all_counties.append(county)
    return all_counties


def get_detection_threshold(disease_key: str, state_key: Optional[str] = None) -> dict:
    """Get detection threshold for a disease, with optional state override.

    Args:
        disease_key: Disease identifier
        state_key: If provided, check for state-level override first

    Returns:
        dict with threshold_pct_ed_visits, require_rising_trend, etc.
    """
    disease = get_disease_config(disease_key)
    threshold = disease["detection"].copy()

    # Check for state-level override
    if state_key:
        state = get_state_config(state_key)
        override = (
            state.get("disease_overrides", {})
            .get(disease_key, {})
            .get("threshold_override")
        )
        if override is not None:
            threshold["threshold_pct_ed_visits"] = override

    return threshold


# === Validation ===


def _validate_state_config(config: dict):
    """Validate required fields in a state config."""
    required = ["state_key", "state_name", "state_abbreviation", "sentinel_metros"]
    missing = [f for f in required if f not in config]
    if missing:
        raise ConfigLoadError(
            f"State config '{config.get('state_key', 'unknown')}' missing required fields: {missing}"
        )
    if not config.get("sentinel_metros"):
        raise ConfigLoadError(
            f"State config '{config['state_key']}' has no sentinel_metros defined"
        )


def _validate_disease_config(config: dict):
    """Validate required fields in a disease config."""
    required = ["disease_key", "display_name", "detection", "data_sources", "severity_classification"]
    missing = [f for f in required if f not in config]
    if missing:
        raise ConfigLoadError(
            f"Disease config '{config.get('disease_key', 'unknown')}' missing required fields: {missing}"
        )
    if "threshold_pct_ed_visits" not in config.get("detection", {}):
        raise ConfigLoadError(
            f"Disease config '{config['disease_key']}' missing detection.threshold_pct_ed_visits"
        )
