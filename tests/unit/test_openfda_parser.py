"""Unit tests for openFDA response parser module.

Tests the parse_openfda_response(), infer_therapeutic_category(),
get_current_epiweek(), and _map_supply_status() functions.

Field mappings reflect the live openFDA Drug Shortages schema:
    - identifier: package_ndc (or openfda.product_ndc[0])
    - name: generic_name (or openfda.brand_name[0])
    - supply: availability + status
    - reason: shortage_reason
    - category: record's therapeutic_category list, else name-based inference
"""
import re
import pytest
from unittest.mock import patch

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAMBDAS = os.path.join(ROOT, "lambdas")
SHARED = os.path.join(LAMBDAS, "shared")
PARSER_DIR = os.path.join(LAMBDAS, "ingestion", "openfda_shortage_fetcher")

if SHARED not in sys.path:
    sys.path.insert(0, SHARED)
if PARSER_DIR not in sys.path:
    sys.path.insert(0, PARSER_DIR)

from parser import (
    parse_openfda_response,
    infer_therapeutic_category,
    get_current_epiweek,
    _map_supply_status,
)


# ── Fixtures ────────────────────────────────────────────────────────────

DETERMINISTIC_EPIWEEK = "2024-W03"


@pytest.fixture
def therapeutic_config():
    """Therapeutic categories config fixture for tests."""
    return {
        "categories": [
            {
                "category_key": "antivirals",
                "display_name": "Antivirals",
                "priority_level": "HIGH",
                "relevant_diseases": ["influenza", "covid"],
                "fda_classification_mapping": [
                    "*oseltamivir*",
                    "*tamiflu*",
                    "*zanamivir*",
                ],
            },
            {
                "category_key": "antibiotics",
                "display_name": "Antibiotics",
                "priority_level": "HIGH",
                "relevant_diseases": ["influenza", "covid", "rsv"],
                "fda_classification_mapping": [
                    "*amoxicillin*",
                    "*azithromycin*",
                    "*penicillin*",
                ],
            },
            {
                "category_key": "respiratory",
                "display_name": "Respiratory Medications",
                "priority_level": "MEDIUM",
                "relevant_diseases": ["rsv", "influenza", "covid"],
                "fda_classification_mapping": [
                    "*albuterol*",
                    "*budesonide*",
                ],
            },
        ]
    }


@pytest.fixture(autouse=True)
def mock_epiweek():
    """Patch get_current_epiweek to return a deterministic value."""
    with patch("parser.get_current_epiweek", return_value=DETERMINISTIC_EPIWEEK):
        yield


# ── Tests: parse_openfda_response ───────────────────────────────────────


class TestParseValidResponse:
    """Test normalization of valid openFDA API responses."""

    def test_parse_valid_response(self, therapeutic_config):
        """Valid openFDA response with all fields returns normalized records
        with correct field mapping."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "ABC-123",
                    "generic_name": "Oseltamivir Capsules, 75mg",
                    "availability": "Available",
                    "status": "Current",
                    "shortage_reason": "Increased demand",
                    # openFDA's broad clinical category is ignored in favour of
                    # name-based inference into the config's category_key taxonomy.
                    "therapeutic_category": ["Antivirals"],
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        record = records[0]
        assert record["product_id"] == "ABC-123"
        assert record["product_name"] == "Oseltamivir Capsules, 75mg"
        assert record["supply_status"] == "AVAILABLE"
        assert record["reason_for_shortage"] == "Increased demand"
        # inferred from the product name -> config category_key
        assert record["therapeutic_category"] == "antivirals"
        assert record["week_timestamp"] == DETERMINISTIC_EPIWEEK

    def test_identifier_falls_back_to_product_ndc(self, therapeutic_config):
        """When package_ndc is missing, the openfda.product_ndc[0] is used."""
        raw_response = {
            "results": [
                {
                    "generic_name": "Amoxicillin",
                    "availability": "Unavailable",
                    "openfda": {"product_ndc": ["64253-400", "64253-401"]},
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["product_id"] == "64253-400"

    def test_name_falls_back_to_openfda_brand(self, therapeutic_config):
        """When top-level generic_name is missing, openfda brand/generic is used."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "DEF-456",
                    "availability": "Available",
                    "openfda": {"brand_name": ["Amoxil"], "generic_name": ["Amoxicillin"]},
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["product_name"] == "Amoxil"

    def test_category_inferred_when_absent(self, therapeutic_config):
        """When the record has no therapeutic_category, it is inferred from name."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "DEF-456",
                    "generic_name": "Amoxicillin Tablets 500mg",
                    "availability": "Discontinued",
                    "shortage_reason": "Manufacturing delay",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["product_name"] == "Amoxicillin Tablets 500mg"
        assert records[0]["therapeutic_category"] == "antibiotics"


class TestSkipInvalidRecords:
    """Test records skipped when missing required fields."""

    def test_skip_record_missing_identifier(self, therapeutic_config, caplog):
        """Records without any NDC identifier are skipped with no error raised."""
        raw_response = {
            "results": [
                {
                    "generic_name": "Some Drug",
                    "availability": "Available",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 0

    def test_skip_record_missing_name(self, therapeutic_config, caplog):
        """Records with an identifier but no resolvable name are skipped."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "GHI-789",
                    "availability": "Available",
                    "shortage_reason": "Unknown",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 0


# ── Tests: _map_supply_status ───────────────────────────────────────────


class TestSupplyStatusMapping:
    """Test mapping of openFDA availability/status values."""

    def test_available_maps_to_available(self):
        """'Available' availability maps to 'AVAILABLE'."""
        assert _map_supply_status("Available") == "AVAILABLE"

    def test_unavailable_maps_to_discontinued(self):
        """'Unavailable' availability maps to 'DISCONTINUED'."""
        assert _map_supply_status("Unavailable") == "DISCONTINUED"

    def test_limited_availability_maps_to_discontinued(self):
        """'Limited Availability' maps to 'DISCONTINUED' (constrained supply)."""
        assert _map_supply_status("Limited Availability") == "DISCONTINUED"

    def test_status_discontinuation_takes_precedence(self):
        """A 'To Be Discontinued' status maps to 'DISCONTINUED'."""
        assert _map_supply_status("Available", "To Be Discontinued") == "DISCONTINUED"

    def test_current_status_without_availability_maps_available(self):
        """No availability but status 'Current' resolves to 'AVAILABLE'."""
        assert _map_supply_status(None, "Current") == "AVAILABLE"

    def test_none_maps_to_unknown(self):
        """None availability and status maps to 'UNKNOWN'."""
        assert _map_supply_status(None) == "UNKNOWN"

    def test_empty_string_maps_to_unknown(self):
        """Empty availability maps to 'UNKNOWN'."""
        assert _map_supply_status("") == "UNKNOWN"


# ── Tests: infer_therapeutic_category ───────────────────────────────────


class TestTherapeuticCategoryInference:
    """Test pattern matching for therapeutic categories."""

    def test_therapeutic_category_inference(self, therapeutic_config):
        """Product names matching patterns in config get classified to
        correct category (e.g., oseltamivir → antivirals)."""
        result = infer_therapeutic_category("Oseltamivir Capsules, 75mg", therapeutic_config)
        assert result == "antivirals"

    def test_antibiotics_pattern(self, therapeutic_config):
        """'Amoxicillin Tablets' maps to 'antibiotics'."""
        result = infer_therapeutic_category("Amoxicillin Tablets 500mg", therapeutic_config)
        assert result == "antibiotics"

    def test_respiratory_pattern(self, therapeutic_config):
        """'Albuterol Inhaler' maps to 'respiratory'."""
        result = infer_therapeutic_category("Albuterol Sulfate Inhaler", therapeutic_config)
        assert result == "respiratory"

    def test_uncategorized_when_no_pattern_match(self, therapeutic_config):
        """Product names not matching any pattern get 'uncategorized'."""
        result = infer_therapeutic_category("Vitamin D Supplement", therapeutic_config)
        assert result == "uncategorized"


# ── Tests: Empty / edge-case responses ──────────────────────────────────


class TestEdgeCases:
    """Test edge cases in the parser."""

    def test_empty_results_array(self, therapeutic_config):
        """Empty results array returns empty list."""
        raw_response = {"results": []}

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert records == []

    def test_reason_defaults_to_unknown(self, therapeutic_config):
        """Missing shortage_reason field defaults to 'Unknown'."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "JKL-101",
                    "generic_name": "Tamiflu Oral Suspension",
                    "availability": "Unavailable",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["reason_for_shortage"] == "Unknown"

    def test_estimated_resolution_date_nullable(self, therapeutic_config):
        """Missing estimated_resolution_date is preserved as None."""
        raw_response = {
            "results": [
                {
                    "package_ndc": "MNO-202",
                    "generic_name": "Azithromycin Tablets, 250mg",
                    "availability": "Available",
                    "shortage_reason": "Demand increase",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["estimated_resolution_date"] is None
