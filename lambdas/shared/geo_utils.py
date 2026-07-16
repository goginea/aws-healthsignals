"""Geographic Utilities — State and county name normalization.

Provides a shared lookup table and normalization functions for converting
external state/county names (from CDC, FDA, state health departments, etc.)
into the internal state keys used by HealthSignals config files.

Usage:
    from shared.geo_utils import normalize_state_name, normalize_state_names

    normalize_state_name("North Carolina")  → "north carolina"
    normalize_state_name("NC")              → "north carolina"
    normalize_state_name("N. Carolina")     → "north carolina"

    normalize_state_names(["Michigan", "OH", "W. Virginia"])
        → ["michigan", "ohio", "west virginia"]
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Comprehensive mapping: various input forms → internal state key
# Covers: full names, 2-letter postal codes, common abbreviations
STATE_LOOKUP: dict[str, str] = {
    # Full names (lowercase)
    "alabama": "alabama", "alaska": "alaska", "arizona": "arizona",
    "arkansas": "arkansas", "california": "california", "colorado": "colorado",
    "connecticut": "connecticut", "delaware": "delaware", "florida": "florida",
    "georgia": "georgia", "hawaii": "hawaii", "idaho": "idaho",
    "illinois": "illinois", "indiana": "indiana", "iowa": "iowa",
    "kansas": "kansas", "kentucky": "kentucky", "louisiana": "louisiana",
    "maine": "maine", "maryland": "maryland", "massachusetts": "massachusetts",
    "michigan": "michigan", "minnesota": "minnesota", "mississippi": "mississippi",
    "missouri": "missouri", "montana": "montana", "nebraska": "nebraska",
    "nevada": "nevada", "new hampshire": "new hampshire", "new jersey": "new jersey",
    "new mexico": "new mexico", "new york": "new york",
    "north carolina": "north carolina", "north dakota": "north dakota",
    "ohio": "ohio", "oklahoma": "oklahoma", "oregon": "oregon",
    "pennsylvania": "pennsylvania", "rhode island": "rhode island",
    "south carolina": "south carolina", "south dakota": "south dakota",
    "tennessee": "tennessee", "texas": "texas", "utah": "utah",
    "vermont": "vermont", "virginia": "virginia", "washington": "washington",
    "west virginia": "west virginia", "wisconsin": "wisconsin", "wyoming": "wyoming",
    "district of columbia": "district of columbia",
    # 2-letter postal codes
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia",
    "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia",
    # Common abbreviations
    "n. carolina": "north carolina", "n carolina": "north carolina",
    "s. carolina": "south carolina", "s carolina": "south carolina",
    "n. dakota": "north dakota", "n dakota": "north dakota",
    "s. dakota": "south dakota", "s dakota": "south dakota",
    "n. hampshire": "new hampshire", "n hampshire": "new hampshire",
    "n. jersey": "new jersey", "n jersey": "new jersey",
    "n. mexico": "new mexico", "n mexico": "new mexico",
    "n. york": "new york", "n york": "new york",
    "w. virginia": "west virginia", "w virginia": "west virginia",
    "r. island": "rhode island", "r island": "rhode island",
    "d.c.": "district of columbia", "d.c": "district of columbia",
}

# Reverse lookup: state key → 2-letter postal code
STATE_TO_POSTAL: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def normalize_state_name(raw: str) -> Optional[str]:
    """Normalize a single state name to its internal state key.

    Args:
        raw: State name in any supported form (full name, postal code, abbreviation).

    Returns:
        Lowercase state key (e.g., "north carolina"), or None if unrecognized.
    """
    key = raw.strip().lower().rstrip(".")
    return STATE_LOOKUP.get(key)


def normalize_state_names(raw_states: list[str]) -> list[str]:
    """Normalize a list of state names to lowercase state keys.

    Handles: full names, 2-letter postal codes, common abbreviations.
    Deduplicates results. Skips unrecognized values with a warning.

    Args:
        raw_states: List of state names in any supported form.

    Returns:
        Deduplicated list of normalized state keys.
    """
    normalized = []
    for raw in raw_states:
        state_key = normalize_state_name(raw)
        if state_key:
            if state_key not in normalized:
                normalized.append(state_key)
        else:
            logger.warning(f"Unrecognized state name: '{raw}' — skipping")
    return normalized


def state_key_to_postal(state_key: str) -> Optional[str]:
    """Convert internal state key to 2-letter postal code.

    Args:
        state_key: Lowercase state key (e.g., "north carolina").

    Returns:
        2-letter postal code (e.g., "NC"), or None if not found.
    """
    return STATE_TO_POSTAL.get(state_key)


def is_valid_state(raw: str) -> bool:
    """Check if a string is a recognized US state name/code.

    Args:
        raw: State name, postal code, or abbreviation.

    Returns:
        True if the input maps to a known state.
    """
    return normalize_state_name(raw) is not None
