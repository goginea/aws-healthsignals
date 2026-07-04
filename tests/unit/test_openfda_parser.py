"""Unit tests for openFDA response parser module.

Tests the parse_openfda_response(), infer_therapeutic_category(),
get_current_epiweek(), and _map_supply_status() functions.
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
                    "product_id": "ABC-123",
                    "productName": "Oseltamivir Capsules, 75mg",
                    "genericName": "Oseltamivir Phosphate",
                    "currentSupplyStatus": "Available",
                    "reason": "Increased demand",
                    "estimatedResolutionDate": "2024-06-01",
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
        assert record["estimated_resolution_date"] == "2024-06-01"
        assert record["therapeutic_category"] == "antivirals"
        assert record["week_timestamp"] == DETERMINISTIC_EPIWEEK

    def test_fallback_to_generic_name(self, therapeutic_config):
        """When productName is missing but genericName is present, parser
        uses genericName as product_name."""
        raw_response = {
            "results": [
                {
                    "product_id": "DEF-456",
                    "genericName": "Amoxicillin",
                    "currentSupplyStatus": "Discontinued",
                    "reason": "Manufacturing delay",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["product_name"] == "Amoxicillin"
        assert records[0]["therapeutic_category"] == "antibiotics"


class TestSkipInvalidRecords:
    """Test records skipped when missing required fields."""

    def test_skip_record_missing_product_id(self, therapeutic_config, caplog):
        """Records without product_id are skipped with no error raised."""
        raw_response = {
            "results": [
                {
                    "productName": "Some Drug",
                    "currentSupplyStatus": "Available",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 0

    def test_skip_record_missing_both_names(self, therapeutic_config, caplog):
        """Records without productName AND genericName are skipped."""
        raw_response = {
            "results": [
                {
                    "product_id": "GHI-789",
                    "currentSupplyStatus": "Available",
                    "reason": "Unknown",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 0


# ── Tests: _map_supply_status ───────────────────────────────────────────


class TestSupplyStatusMapping:
    """Test mapping of openFDA supply status values."""

    def test_available_maps_to_available(self):
        """'Available' maps to 'AVAILABLE'."""
        assert _map_supply_status("Available") == "AVAILABLE"

    def test_discontinued_maps_to_discontinued(self):
        """'Discontinued' maps to 'DISCONTINUED'."""
        assert _map_supply_status("Discontinued") == "DISCONTINUED"

    def test_none_maps_to_unknown(self):
        """None maps to 'UNKNOWN'."""
        assert _map_supply_status(None) == "UNKNOWN"

    def test_empty_string_maps_to_unknown(self):
        """Empty string maps to 'UNKNOWN'."""
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
        """Missing reason field defaults to 'Unknown'."""
        raw_response = {
            "results": [
                {
                    "product_id": "JKL-101",
                    "productName": "Tamiflu Oral Suspension",
                    "currentSupplyStatus": "Discontinued",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["reason_for_shortage"] == "Unknown"

    def test_estimated_resolution_date_nullable(self, therapeutic_config):
        """Null/missing estimated_resolution_date is preserved as None."""
        raw_response = {
            "results": [
                {
                    "product_id": "MNO-202",
                    "productName": "Azithromycin Tablets, 250mg",
                    "currentSupplyStatus": "Available",
                    "reason": "Demand increase",
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["estimated_resolution_date"] is None

    def test_estimated_resolution_date_explicit_none(self, therapeutic_config):
        """Explicitly null estimatedResolutionDate is preserved as None."""
        raw_response = {
            "results": [
                {
                    "product_id": "PQR-303",
                    "productName": "Penicillin V Potassium",
                    "currentSupplyStatus": "Available",
                    "reason": "Supply disruption",
                    "estimatedResolutionDate": None,
                }
            ]
        }

        records = parse_openfda_response(raw_response, therapeutic_config)

        assert len(records) == 1
        assert records[0]["estimated_resolution_date"] is None
