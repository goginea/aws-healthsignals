"""Unit tests for leader detection Lambda (config-driven version)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

import sys
sys.path.insert(0, "lambdas/prediction/leader_detection")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")


# Mock config_loader at module level before handler import
mock_system_config = {
    "dynamodb_tables": {"alert_state": "healthsignals-alert-state-test"}
}

mock_threshold = {
    "threshold_pct_ed_visits": 1.0,
    "require_rising_trend": True,
}

mock_metros = {
    "26420": {"short_name": "Houston", "state_abbreviation": "TX"},
    "19100": {"short_name": "DFW", "state_abbreviation": "TX"},
    "12420": {"short_name": "Austin", "state_abbreviation": "TX"},
    "41700": {"short_name": "San Antonio", "state_abbreviation": "TX"},
}


@pytest.fixture(autouse=True)
def mock_config():
    """Mock config_loader and DynamoDB for all tests."""
    with patch("shared.config_loader.get_system_config", return_value=mock_system_config), \
         patch("shared.config_loader.get_detection_threshold", return_value=mock_threshold), \
         patch("shared.config_loader.get_all_sentinel_metros", return_value=mock_metros), \
         patch("shared.config_loader.get_disease_config", return_value={}):
        yield


# Import AFTER mocks are in place
with patch("shared.config_loader.get_system_config", return_value=mock_system_config), \
     patch("shared.config_loader.get_detection_threshold", return_value=mock_threshold), \
     patch("shared.config_loader.get_all_sentinel_metros", return_value=mock_metros), \
     patch("boto3.resource"):
    from handler import (
        lambda_handler,
        is_threshold_crossed,
        determine_season,
    )


class TestThresholdCrossing:
    """Test the core threshold detection logic."""

    def test_flu_threshold_crossed_rising(self):
        """Flu at 1.5% with rising trend should cross threshold."""
        assert is_threshold_crossed(1.5, "rising", 1.0, True) is True

    def test_flu_below_threshold(self):
        """Flu at 0.8% should NOT cross threshold."""
        assert is_threshold_crossed(0.8, "rising", 1.0, True) is False

    def test_flu_above_threshold_not_rising(self):
        """Flu at 1.5% but declining should NOT cross (require_rising=True)."""
        assert is_threshold_crossed(1.5, "declining", 1.0, True) is False

    def test_flu_above_threshold_flat(self):
        """Flu at 1.5% but flat trend should NOT cross (require_rising=True)."""
        assert is_threshold_crossed(1.5, "flat", 1.0, True) is False

    def test_exactly_at_threshold(self):
        """Signal exactly at threshold should cross."""
        assert is_threshold_crossed(1.0, "rising", 1.0, True) is True

    def test_covid_lower_threshold(self):
        """COVID has lower threshold (0.3%)."""
        assert is_threshold_crossed(0.4, "rising", 0.3, True) is True
        assert is_threshold_crossed(0.2, "rising", 0.3, True) is False

    def test_no_rising_requirement(self):
        """When require_rising=False, any trend is accepted."""
        assert is_threshold_crossed(1.5, "declining", 1.0, False) is True
        assert is_threshold_crossed(1.5, "flat", 1.0, False) is True


class TestSeasonDetermination:
    """Test epiweek to season mapping."""

    def test_fall_start_week_40(self):
        """Week 40+ is start of new season."""
        assert determine_season("202640") == "2026-27"

    def test_fall_week_52(self):
        """Week 52 is still current season."""
        assert determine_season("202652") == "2026-27"

    def test_spring_week_10(self):
        """Week 10 is end of previous season."""
        assert determine_season("202710") == "2026-27"

    def test_summer_week_30(self):
        """Week 30 maps to previous fall season."""
        assert determine_season("202630") == "2025-26"


class TestLeaderDetectionHandler:
    """Integration tests for the full handler."""

    @patch("handler.check_existing_alert")
    @patch("handler.record_leader_detection")
    @patch("handler.get_detection_threshold", return_value=mock_threshold)
    @patch("handler.get_all_sentinel_metros", return_value=mock_metros)
    def test_single_leader_detected(self, mock_metros_fn, mock_thresh, mock_record, mock_check):
        """When Houston crosses threshold, it should be detected as leader."""
        mock_check.return_value = False

        event = {
            "disease": "influenza",
            "week": "202645",
            "metro_signals": {
                "26420": {"value": 3.2, "trend": "rising"},
                "19100": {"value": 0.8, "trend": "flat"},
                "12420": {"value": 0.4, "trend": "flat"},
                "41700": {"value": 0.6, "trend": "rising"},
            },
        }

        result = lambda_handler(event, None)

        assert result["detected"] is True
        assert result["new_alert"] is True
        assert result["leader"]["msa_code"] == "26420"
        assert result["leader"]["value"] == 3.2
        assert result["trigger_prediction_pipeline"] is True

    @patch("handler.get_detection_threshold", return_value=mock_threshold)
    @patch("handler.get_all_sentinel_metros", return_value=mock_metros)
    def test_no_threshold_crossed(self, mock_metros_fn, mock_thresh):
        """When no metro crosses threshold, no detection."""
        event = {
            "disease": "influenza",
            "week": "202640",
            "metro_signals": {
                "26420": {"value": 0.5, "trend": "flat"},
                "19100": {"value": 0.3, "trend": "flat"},
            },
        }

        result = lambda_handler(event, None)

        assert result["detected"] is False

    @patch("handler.check_existing_alert")
    @patch("handler.record_leader_detection")
    @patch("handler.get_detection_threshold", return_value=mock_threshold)
    @patch("handler.get_all_sentinel_metros", return_value=mock_metros)
    def test_multiple_metros_crossed(self, mock_metros_fn, mock_thresh, mock_record, mock_check):
        """When multiple metros cross, highest value is primary leader."""
        mock_check.return_value = False

        event = {
            "disease": "influenza",
            "week": "202650",
            "metro_signals": {
                "26420": {"value": 4.5, "trend": "rising"},
                "19100": {"value": 8.2, "trend": "rising"},
                "12420": {"value": 2.1, "trend": "rising"},
            },
        }

        result = lambda_handler(event, None)

        assert result["detected"] is True
        assert result["leader"]["msa_code"] == "19100"  # DFW highest
        assert len(result["all_leaders"]) == 3

    @patch("handler.check_existing_alert")
    @patch("handler.get_detection_threshold", return_value=mock_threshold)
    @patch("handler.get_all_sentinel_metros", return_value=mock_metros)
    def test_duplicate_detection_suppressed(self, mock_metros_fn, mock_thresh, mock_check):
        """Already-alerted leader should not trigger new alert."""
        mock_check.return_value = True  # Already alerted

        event = {
            "disease": "influenza",
            "week": "202646",
            "metro_signals": {
                "26420": {"value": 5.0, "trend": "rising"},
            },
        }

        result = lambda_handler(event, None)

        assert result["detected"] is True
        assert result["new_alert"] is False
