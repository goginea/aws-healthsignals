"""OpenFDA Response Parser — Transforms raw openFDA Drug Shortages API responses
into normalized shortage records for downstream change detection and DynamoDB storage.

Field mapping (aligned to the live openFDA Drug Shortages schema):
    - product_id: package_ndc, else openfda.product_ndc[0]
    - product_name: generic_name, else openfda.brand_name[0]
    - availability/status → supply_status (AVAILABLE / DISCONTINUED / UNKNOWN)
    - shortage_reason → reason_for_shortage (default "Unknown")
    - estimated resolution: not provided by the API (nullable)
    - therapeutic_category: from the record's therapeutic_category list when
      present, else inferred via pattern matching against config
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
        openfda = record.get("openfda", {}) or {}

        # Resolve product identifier. The openFDA shortages schema does not
        # expose a top-level "product_id"; the stable identifier is the NDC.
        product_id = record.get("package_ndc") or _first(openfda.get("product_ndc"))
        if not product_id:
            logger.warning("Skipping record without an NDC identifier: %s", record)
            continue

        # Resolve product name: top-level generic_name, else openfda brand/generic.
        product_name = (
            record.get("generic_name")
            or _first(openfda.get("brand_name"))
            or _first(openfda.get("generic_name"))
        )
        if not product_name:
            logger.warning(
                "Skipping record without a product name: product_id=%s", product_id
            )
            continue

        # Classify into a monitored category_key via name-based inference.
        # openFDA's own therapeutic_category values (e.g. "Cardiovascular",
        # "Neurology") are broad clinical areas that do not correspond to the
        # config's category_key taxonomy (antivirals, antibiotics, ...), so the
        # downstream monitored-category filter keys off the inferred value.
        therapeutic_category = infer_therapeutic_category(product_name, therapeutic_config)

        normalized.append({
            "product_id": product_id,
            "product_name": product_name,
            "supply_status": _map_supply_status(
                record.get("availability"), record.get("status")
            ),
            "reason_for_shortage": record.get("shortage_reason", "Unknown"),
            "estimated_resolution_date": record.get("estimated_resolution_date"),
            "therapeutic_category": therapeutic_category,
            "week_timestamp": week_timestamp,
        })

    return normalized


def _first(value: Any) -> Any:
    """Return the first element of a list, or the value itself if not a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


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


def _map_supply_status(availability: str | None, status: str | None = None) -> str:
    """Map openFDA availability/status fields to internal status codes.

    The live openFDA shortages schema uses two fields:
      - ``availability``: e.g. "Available", "Unavailable", "Limited Availability"
      - ``status``: e.g. "Current", "To Be Discontinued", "Resolved"

    A discontinuation (from ``status``) takes precedence, since it is the most
    material supply signal. Otherwise availability drives the mapping.

    Returns:
        One of "AVAILABLE", "DISCONTINUED", or "UNKNOWN".
    """
    status_norm = (status or "").strip().lower()
    if "discontinu" in status_norm:
        return "DISCONTINUED"

    avail_norm = (availability or "").strip().lower()
    if not avail_norm:
        # No availability signal — fall back to status if it is informative.
        return "AVAILABLE" if status_norm == "current" else "UNKNOWN"
    if "unavailable" in avail_norm or "limited" in avail_norm:
        return "DISCONTINUED"
    if "available" in avail_norm:
        return "AVAILABLE"
    return "UNKNOWN"
