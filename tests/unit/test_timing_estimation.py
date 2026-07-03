"""Unit tests for timing estimation Lambda (config-driven version)."""
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

import sys
sys.path.insert(0, "lambdas/prediction/timing_estimation")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")

# Mock config before import
mock_system_config = {
    "dynamodb_tables": {"calibration": "healthsignals-calibration-test"}
}

mock_disease_config = {
    "prediction": {
        "typical_lag_range_weeks": [3, 6],
        "severity_multiplier_typical_range": [1.0, 3.0],
        "confidence_degrades_after_seasons": 2,
    }
}

with patch("shared.config_loader.get_system_config", return_value=mock_system_config), \
     patch("shared.config_loader.get_disease_config", return_value=mock_disease_config), \
     patch("boto3.resource"):
    from handler import (
        lambda_handler,
        calculate_lag_estimate,
        calculate_severity_multiplier,
        calculate_confidence,
        get_calibration_data,
    )


class TestLagEstimate:
    """Test historical lag calculation."""

    def test_three_season_median(self):
        """With 3 seasons, should return correct median."""
        calibration = [
            {"lag_weeks": Decimal("4")},
            {"lag_weeks": Decimal("5")},
            {"lag_weeks": Decimal("4")},
        ]
        result = calculate_lag_estimate(calibration)

        assert result["median"] == 4.0
        assert result["min"] <= result["median"]
        assert result["max"] >= result["median"]

    def test_two_season_min_max(self):
        """With 2 seasons, uses min/max directly."""
        calibration = [
            {"lag_weeks": Decimal("3")},
            {"lag_weeks": Decimal("6")},
        ]
        result = calculate_lag_estimate(calibration)

        assert result["median"] == 4.5  # median of [3, 6]
        assert result["min"] == 3.0
        assert result["max"] == 6.0

    def test_empty_calibration_defaults(self):
        """Empty calibration should return safe defaults."""
        result = calculate_lag_estimate([])

        assert result["median"] == 4.0
        assert result["min"] == 3.0
        assert result["max"] == 6.0

    def test_single_season(self):
        """Single season — min and max are both the same value."""
        calibration = [{"lag_weeks": Decimal("5")}]
        result = calculate_lag_estimate(calibration)

        assert result["median"] == 5.0
        assert result["min"] == 5.0
        assert result["max"] == 5.0


class TestSeverityMultiplier:
    """Test severity multiplier calculation."""

    def test_three_season_median(self):
        """With 3 seasons, should return correct median."""
        calibration = [
            {"severity_multiplier": Decimal("1.8")},
            {"severity_multiplier": Decimal("2.1")},
            {"severity_multiplier": Decimal("1.6")},
        ]
        result = calculate_severity_multiplier(calibration)

        assert result["median"] == 1.8  # median of [1.6, 1.8, 2.1]

    def test_empty_calibration_defaults(self):
        """Empty should return safe defaults."""
        result = calculate_severity_multiplier([])

        assert result["median"] == 2.0

    def test_min_floor(self):
        """Min should never go below 0.5."""
        calibration = [
            {"severity_multiplier": Decimal("0.3")},
            {"severity_multiplier": Decimal("0.4")},
            {"severity_multiplier": Decimal("0.2")},
        ]
        result = calculate_severity_multiplier(calibration)

        assert result["min"] >= 0.5


class TestConfidence:
    """Test confidence score calculation."""

    def test_five_seasons_high_confidence(self):
        """5+ seasons should have high confidence."""
        calibration = [{"lag_weeks": i} for i in range(5)]
        conf = calculate_confidence(calibration, 2)
        assert conf == 0.85

    def test_three_seasons_moderate_confidence(self):
        """3-4 seasons should have moderate confidence."""
        calibration = [{"lag_weeks": i} for i in range(3)]
        conf = calculate_confidence(calibration, 2)
        assert conf == 0.70

    def test_two_seasons_lower_confidence(self):
        """2 seasons should have lower confidence."""
        calibration = [{"lag_weeks": i} for i in range(2)]
        conf = calculate_confidence(calibration, 2)
        assert conf == 0.55

    def test_one_season_low_confidence(self):
        """1 season should have low confidence."""
        calibration = [{"lag_weeks": 1}]
        conf = calculate_confidence(calibration, 2)
        assert conf == 0.30


class TestFullHandler:
    """Integration tests for the timing estimation handler."""

    @patch("handler.get_calibration_data")
    @patch("handler.get_disease_config", return_value=mock_disease_config)
    def test_handler_with_calibration(self, mock_disease, mock_calibration):
        """Handler should use calibration data when available."""
        mock_calibration.return_value = [
            {"lag_weeks": Decimal("4"), "severity_multiplier": Decimal("2.0")},
            {"lag_weeks": Decimal("5"), "severity_multiplier": Decimal("1.8")},
        ]

        event = {
            "leader_msa": "26420",
            "disease": "influenza",
            "week": "202645",
            "affected_counties": [
                {
                    "county_fips": "48143",
                    "county_name": "Erath County",
                    "affinity_weight": 0.75,
                }
            ],
        }

        result = lambda_handler(event, None)

        assert result["total_counties"] == 1
        assert result["estimates"][0]["county_name"] == "Erath County"
        assert result["estimates"][0]["data_source"] == "historical_calibration"
        assert result["estimates"][0]["confidence"] >= 0.5

    @patch("handler.get_calibration_data")
    @patch("handler.get_disease_config", return_value=mock_disease_config)
    def test_handler_with_no_calibration(self, mock_disease, mock_calibration):
        """Handler should use disease defaults when no calibration exists."""
        mock_calibration.return_value = []

        event = {
            "leader_msa": "26420",
            "disease": "influenza",
            "week": "202645",
            "affected_counties": [
                {
                    "county_fips": "48999",
                    "county_name": "New County",
                    "affinity_weight": 0.5,
                }
            ],
        }

        result = lambda_handler(event, None)

        assert result["estimates"][0]["data_source"] == "disease_defaults"
        assert result["estimates"][0]["confidence"] == 0.3

    @patch("handler.get_disease_config", return_value=mock_disease_config)
    def test_handler_no_counties(self, mock_disease):
        """Handler should handle empty county list gracefully."""
        event = {
            "leader_msa": "26420",
            "disease": "influenza",
            "week": "202645",
            "affected_counties": [],
        }

        result = lambda_handler(event, None)

        assert result["estimates"] == []
        assert "No affected counties" in result["reason"]
