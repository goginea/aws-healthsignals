"""Unit tests for Geographic Affinity Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas/prediction/geographic_affinity"))


# Sample metro and county config data for mocking
MOCK_METROS = {
    "26420": {"short_name": "Houston", "full_name": "Houston-The Woodlands-Sugar Land"},
    "19100": {"short_name": "DFW", "full_name": "Dallas-Fort Worth-Arlington"},
}

MOCK_COUNTIES = [
    {
        "county_fips": "48143",
        "county_name": "Erath County",
        "population": 42000,
        "_state_key": "texas",
        "primary_metro_affinity": "19100",
        "affinity_weights": {"19100": 1.0, "26420": 0.3},
        "delivery_preferences": {"channels": ["email", "sms"]},
        "contacts": {"primary_email": "health@erath.gov"},
    },
    {
        "county_fips": "48439",
        "county_name": "Tarrant County",
        "population": 2100000,
        "_state_key": "texas",
        "primary_metro_affinity": "19100",
        "affinity_weights": {"19100": 0.9},
        "delivery_preferences": {"channels": ["email"]},
        "contacts": {"primary_email": "health@tarrant.gov"},
    },
    {
        "county_fips": "48039",
        "county_name": "Brazoria County",
        "population": 375000,
        "_state_key": "texas",
        "primary_metro_affinity": "26420",
        "affinity_weights": {"26420": 0.85},
        "delivery_preferences": {"channels": ["email"]},
        "contacts": {"primary_email": "health@brazoria.gov"},
    },
]


@patch("handler.get_system_config", return_value={"dynamodb_tables": {"county_configs": "test-table"}})
@patch("handler.get_subscribing_counties", return_value=MOCK_COUNTIES)
@patch("handler.get_all_sentinel_metros", return_value=MOCK_METROS)
class TestGeographicAffinity:
    """Test county-to-metro mapping logic."""

    def test_houston_leader_finds_houston_counties(self, mock_metros, mock_counties, mock_sys):
        """When Houston is leader, counties with Houston affinity are returned."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "26420", "value": 3.2},
            "week": "202645",
        }

        result = lambda_handler(event, None)

        assert len(result["affected_counties"]) == 2  # Erath (0.3) + Brazoria (0.85)
        fips_list = [c["county_fips"] for c in result["affected_counties"]]
        assert "48039" in fips_list  # Brazoria (Houston affinity 0.85)
        assert "48143" in fips_list  # Erath (Houston affinity 0.3)
        assert "48439" not in fips_list  # Tarrant has no Houston affinity

    def test_dfw_leader_finds_dfw_counties(self, mock_metros, mock_counties, mock_sys):
        """When DFW is leader, counties with DFW affinity are returned."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "19100", "value": 5.0},
            "week": "202648",
        }

        result = lambda_handler(event, None)

        assert len(result["affected_counties"]) == 2  # Erath (1.0) + Tarrant (0.9)
        fips_list = [c["county_fips"] for c in result["affected_counties"]]
        assert "48143" in fips_list  # Erath (DFW affinity 1.0)
        assert "48439" in fips_list  # Tarrant (DFW affinity 0.9)
        assert "48039" not in fips_list  # Brazoria has no DFW affinity

    def test_sorted_by_affinity_weight_descending(self, mock_metros, mock_counties, mock_sys):
        """Results should be sorted by affinity weight (strongest first)."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "19100", "value": 5.0},
            "week": "202648",
        }

        result = lambda_handler(event, None)
        weights = [c["affinity_weight"] for c in result["affected_counties"]]
        assert weights == sorted(weights, reverse=True)

    def test_unknown_metro_returns_empty(self, mock_metros, mock_counties, mock_sys):
        """Unknown MSA code should return no affected counties."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "99999", "value": 2.0},
            "week": "202645",
        }

        result = lambda_handler(event, None)
        assert result["affected_counties"] == []

    def test_no_leader_msa_returns_empty(self, mock_metros, mock_counties, mock_sys):
        """Missing leader MSA should return empty with reason."""
        from handler import lambda_handler

        event = {"disease": "influenza", "leader": {}, "week": "202645"}

        result = lambda_handler(event, None)
        assert result["affected_counties"] == []
        assert "No leader MSA" in result.get("reason", "")

    def test_primary_affinity_flag(self, mock_metros, mock_counties, mock_sys):
        """is_primary_affinity should be True only when county's primary matches leader."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "26420", "value": 3.2},
            "week": "202645",
        }

        result = lambda_handler(event, None)

        # Brazoria's primary is 26420 (Houston) → True
        brazoria = next(c for c in result["affected_counties"] if c["county_fips"] == "48039")
        assert brazoria["is_primary_affinity"] is True

        # Erath's primary is 19100 (DFW) → False (even though it has Houston affinity)
        erath = next(c for c in result["affected_counties"] if c["county_fips"] == "48143")
        assert erath["is_primary_affinity"] is False

    def test_total_affected_population(self, mock_metros, mock_counties, mock_sys):
        """Total population should sum all affected counties."""
        from handler import lambda_handler

        event = {
            "disease": "influenza",
            "season": "2026-27",
            "leader": {"msa_code": "19100", "value": 5.0},
            "week": "202648",
        }

        result = lambda_handler(event, None)
        # Erath (42000) + Tarrant (2100000) = 2142000
        assert result["total_affected_population"] == 42000 + 2100000
