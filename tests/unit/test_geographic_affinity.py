"""Unit tests for Geographic Affinity Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {"dynamodb_tables": {"county_configs": "healthsignals-county-configs-test"}}
MOCK_STATE = {
    "state_key": "texas",
    "sentinel_metros": {
        "26420": {
            "short_name": "Houston",
            "county_fips": ["48201", "48157"],
        }
    },
    "subscribing_counties": [
        {
            "county_fips": "48143",
            "county_name": "Erath County",
            "primary_metro_affinity": "26420",
            "affinity_weights": {"26420": 0.8, "19100": 0.2},
            "expected_lag_weeks": 4.5,
            "alert_contacts": [{"type": "email", "value": "health@erath.gov"}],
        }
    ],
}


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "prediction/geographic_affinity",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_state_config": MOCK_STATE,
            "shared.config_loader.list_active_states": [MOCK_STATE],
            "shared.config_loader.get_subscribing_counties": MOCK_STATE["subscribing_counties"],
            "boto3.resource": MagicMock(),
        },
    )


class TestGeographicAffinity:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_handler_with_leader(self, handler):
        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "26420", "value": 3.2},
            "week": "202645",
        }
        result = handler.lambda_handler(event, None)
        assert "affected_counties" in result
        assert isinstance(result["affected_counties"], list)

    def test_handler_no_leader(self, handler):
        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": None,
            "week": "202645",
        }
        result = handler.lambda_handler(event, None)
        affected = result.get("affected_counties", [])
        assert len(affected) == 0

    def test_handler_unknown_metro(self, handler):
        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "99999", "value": 2.0},
            "week": "202645",
        }
        result = handler.lambda_handler(event, None)
        affected = result.get("affected_counties", [])
        # Unknown metro should return empty or gracefully handle
        assert isinstance(affected, list)
