"""Unit tests for Outbreak Processor Lambda.

Tests state name normalization, county resolution, and SFN invocation.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import load_handler


MOCK_SYSTEM = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"},
}

MOCK_STATE_CONFIG_TEXAS = {
    "state_key": "texas",
    "state_name": "Texas",
    "enabled": True,
    "subscribing_counties": [
        {"county_fips": "48143", "county_name": "Erath County"},
        {"county_fips": "48251", "county_name": "Johnson County"},
    ],
}


@pytest.fixture(scope="module")
def handler():
    os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123:stateMachine:test"

    return load_handler(
        "orchestration/outbreak_processor",
        extra_patches={
            "shared.config_loader.list_active_states": [
                {"state_key": "texas"},
                {"state_key": "ohio"},
            ],
            "shared.config_loader.get_state_config": MOCK_STATE_CONFIG_TEXAS,
            "boto3.client": MagicMock(),
        },
    )


class TestStateNameNormalization:
    """Test the STATE_LOOKUP and normalize_state_names function."""

    def test_full_name_lowercase(self, handler):
        """Full state name normalizes correctly."""
        result = handler.normalize_state_names(["Michigan", "Ohio"])
        assert result == ["michigan", "ohio"]

    def test_postal_code(self, handler):
        """2-letter postal codes normalize correctly."""
        result = handler.normalize_state_names(["TX", "OH", "MI"])
        assert result == ["texas", "ohio", "michigan"]

    def test_abbreviated_forms(self, handler):
        """Common abbreviations normalize correctly."""
        result = handler.normalize_state_names(["N. Carolina", "W. Virginia"])
        assert "north carolina" in result
        assert "west virginia" in result

    def test_case_insensitive(self, handler):
        """Normalization is case-insensitive."""
        result = handler.normalize_state_names(["TEXAS", "texas", "Texas"])
        # Should deduplicate
        assert result == ["texas"]

    def test_unrecognized_state_skipped(self, handler):
        """Unrecognized names are skipped with warning."""
        result = handler.normalize_state_names(["Michigan", "Narnia", "Ohio"])
        assert result == ["michigan", "ohio"]

    def test_empty_list(self, handler):
        """Empty input returns empty list."""
        result = handler.normalize_state_names([])
        assert result == []

    def test_whitespace_stripped(self, handler):
        """Whitespace around state names is stripped."""
        result = handler.normalize_state_names(["  Texas  ", " Ohio\n"])
        assert result == ["texas", "ohio"]

    def test_district_of_columbia(self, handler):
        """DC variants normalize correctly."""
        result = handler.normalize_state_names(["D.C.", "DC", "District of Columbia"])
        assert result == ["district of columbia"]

    def test_west_virginia_not_virginia(self, handler):
        """West Virginia is distinct from Virginia."""
        result = handler.normalize_state_names(["West Virginia", "Virginia"])
        assert "west virginia" in result
        assert "virginia" in result
        assert len(result) == 2


class TestOutbreakProcessing:
    """Test the main processor logic."""

    def test_processes_affected_states(self, handler):
        """Resolves affected states to counties and starts SFN."""
        handler.sfn_client = MagicMock()
        handler.sfn_client.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:test:outbreak-texas-123"
        }

        event = {
            "outbreak_id": "cyclospora-test-abc123",
            "title": "Cyclosporiasis Outbreak",
            "disease_name": "Cyclosporiasis",
            "affected_states": ["Texas"],
            "previous_states": [],
            "case_count": 400,
            "source_food": "Unknown",
            "onset_date": "2026-06-22",
            "status": "active",
            "summary": "Test outbreak",
            "cdc_link": "https://cdc.gov/test",
        }

        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["alerts_started"] == 1
        handler.sfn_client.start_execution.assert_called_once()

    def test_unmonitored_state_skipped(self, handler):
        """States not in our config are skipped."""
        handler.sfn_client = MagicMock()

        event = {
            "outbreak_id": "test-xyz",
            "title": "Test Outbreak",
            "disease_name": "E. coli",
            "affected_states": ["California"],  # Not in our mock active states
            "previous_states": [],
            "case_count": 10,
        }

        result = handler.lambda_handler(event, None)

        assert result["alerts_started"] == 0
        handler.sfn_client.start_execution.assert_not_called()

    def test_no_affected_states_returns_early(self, handler):
        """Empty affected_states returns immediately."""
        event = {
            "outbreak_id": "test-empty",
            "title": "Empty Outbreak",
            "affected_states": [],
            "previous_states": [],
        }

        result = handler.lambda_handler(event, None)

        assert result["alerts_started"] == 0
        assert result["reason"] == "no_affected_states"

    def test_new_states_identified_on_update(self, handler):
        """Update events correctly identify newly added states."""
        handler.sfn_client = MagicMock()
        handler.sfn_client.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:test:outbreak-texas-123"
        }

        event = {
            "outbreak_id": "update-test",
            "title": "Updated Outbreak",
            "disease_name": "Cyclospora",
            "affected_states": ["Texas", "Ohio"],
            "previous_states": ["Ohio"],  # Ohio was already known
            "case_count": 500,
            "status": "active",
        }

        result = handler.lambda_handler(event, None)

        # Texas is new, Ohio was already known
        assert "texas" in result["new_states"]
        assert "ohio" not in result["new_states"]
