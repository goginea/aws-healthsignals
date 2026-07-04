"""OpenFDA Response Parser — Transforms raw openFDA Drug Shortages API responses
into normalized shortage records for downstream change detection and DynamoDB storage.

Field mapping:
    - product_id: from openFDA "product_id" field
    - productName → product_name (fallback to genericName)
    - currentSupplyStatus → supply_status (AVAILABLE / DISCONTINUED / UNKNOWN)
    - reason → reason_for_shortage (default "Unknown")
    - estimatedResolutionDate → estimated_resolution_date (nullable)
    - therapeutic_category: inferred via pattern matching against config
    - week_timestamp: current ISO epiweek (YYYY-Www)
"""
import fnmatch
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def parse_openfda_response(raw_response: dict, therapeutic_config: dict) -> list[dict]:
    """Parse and normalize openFDA Drug Shortages API response.

    Args:
        raw_response: Raw JSON response from openFDA with "results" array.
        therapeutic_config: Loaded therapeutic_categories.json config dict.

    Returns:
        List of normalized shortage record dicts ready for DynamoDB storage.
        Records missing both productName and genericName, or missing product_id,
        are skipped with a warning log.
    """
    results = raw_response.get("results", [])
    normalized: list[dict] = []
    week_timestamp = get_current_epiweek()

    for record in results:
        # Skip records without product identifier
        product_id = record.get("product_id")
        if not product_id:
            logger.warning("Skipping record without product_id: %s", record)
            continue

        # Resolve product name with fallback
        product_name = record.get("productName") or record.get("genericName")
        if not product_name:
            logger.warning(
                "Skipping record without productName or genericName: product_id=%s",
                product_id,
            )
            continue

        # Infer therapeutic category from product name
        therapeutic_category = infer_therapeutic_category(product_name, therapeutic_config)

        normalized.append({
            "product_id": product_id,
            "product_name": product_name,
            "supply_status": _map_supply_status(record.get("currentSupplyStatus")),
            "reason_for_shortage": record.get("reason", "Unknown"),
            "estimated_resolution_date": record.get("estimatedResolutionDate"),
            "therapeutic_category": therapeutic_category,
            "week_timestamp": week_timestamp,
        })

    return normalized


def infer_therapeutic_category(product_name: str, config: dict) -> str:
    """Infer therapeutic category using fnmatch-style glob pattern matching.

    Matches the product name (case-insensitive) against the
    fda_classification_mapping patterns defined in each category of the config.

    Args:
        product_name: Drug product name to classify.
        config: Loaded therapeutic_categories.json config dict with "categories" array.

    Returns:
        The category_key of the matched category, or "uncategorized" if no match.
    """
    name_lower = product_name.lower()
    categories = config.get("categories", [])

    for category in categories:
        patterns = category.get("fda_classification_mapping", [])
        for pattern in patterns:
            if fnmatch.fnmatch(name_lower, pattern.lower()):
                return category["category_key"]

    return "uncategorized"


def get_current_epiweek() -> str:
    """Get the current epidemiological week in ISO format YYYY-Www.

    Uses ISO 8601 week numbering (Monday-based weeks).

    Returns:
        ISO week string, e.g. "2024-W03".
    """
    now = datetime.utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _map_supply_status(raw_status: str | None) -> str:
    """Map openFDA currentSupplyStatus to internal status codes.

    Mapping:
        "Available" → "AVAILABLE"
        "Discontinued" → "DISCONTINUED"
        empty/missing/unrecognized → "UNKNOWN"

    Args:
        raw_status: The currentSupplyStatus value from the API response.

    Returns:
        One of "AVAILABLE", "DISCONTINUED", or "UNKNOWN".
    """
    if not raw_status:
        return "UNKNOWN"

    status_map = {
        "available": "AVAILABLE",
        "discontinued": "DISCONTINUED",
    }

    return status_map.get(raw_status.strip().lower(), "UNKNOWN")
