"""Unit tests for leader detection Lambda (config-driven version)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

from tests.conftest import load_handler


# ── Mock configs ─────────────────────────────────────────────────────────
MOCK_SYSTEM = {
    "dynamodb_tables": {"alert_state": "healthsignals-alert-state-test"}
}

MOCK_THRESHOLD = {
    "threshold_pct_ed_visits": 1.0,
    "require_rising_trend": True,
}

MOCK_METROS = {
    "26420": {"short_name": "Houston", "state_abbreviation": "TX"},
    "19100": {"short_name": "DFW", "state_abbreviation": "TX"},
    "12420": {"short_name": "Austin", "state_abbreviation": "TX"},
    "41700": {"short_name": "San Antonio", "state_abbreviation": "TX"},
}


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "prediction/leader_detection",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_disease_config": {},
            "shared.config_loader.get_detection_threshold": MOCK_THRESHOLD,
            "shared.config_loader.get_all_sentinel_metros": MOCK_METROS,
            "boto3.resource": MagicMock(),
        },
    )


class TestThresholdCrossing:
    """Test the core threshold detection logic."""

    def test_flu_threshold_crossed_rising(self, handler):
        assert handler.is_threshold_crossed(1.5, "rising", 1.0, True) is True

    def test_flu_below_threshold(self, handler):
        assert handler.is_threshold_crossed(0.8, "rising", 1.0, True) is False

    def test_flu_above_threshold_not_rising(self, handler):
        assert handler.is_threshold_crossed(1.5, "declining", 1.0, True) is False

    def test_flu_above_threshold_flat(self, handler):
        assert handler.is_threshold_crossed(1.5, "flat", 1.0, True) is False

    def test_exactly_at_threshold(self, handler):
        assert handler.is_threshold_crossed(1.0, "rising", 1.0, True) is True

    def test_covid_lower_threshold(self, handler):
        assert handler.is_threshold_crossed(0.4, "rising", 0.3, True) is True
        assert handler.is_threshold_crossed(0.2, "rising", 0.3, True) is False

    def test_no_rising_requirement(self, handler):
        """When require_rising is False, flat trend should still cross."""
        assert handler.is_threshold_crossed(2.0, "flat", 1.0, False) is True


class TestSeasonDetermination:
    """Test epiweek to season mapping."""

    def test_fall_start_week_40(self, handler):
        assert handler.determine_season("202640") == "2026-27"

    def test_fall_week_52(self, handler):
        assert handler.determine_season("202652") == "2026-27"

    def test_spring_week_10(self, handler):
        assert handler.determine_season("202710") == "2026-27"

    def test_summer_week_30(self, handler):
        assert handler.determine_season("202630") == "2025-26"


class TestLeaderDetectionHandler:
    """Integration tests for the full handler."""

    def test_single_leader_detected(self, handler):
        """When Houston crosses threshold, it should be detected as leader."""
        with patch.object(handler, "check_existing_alert", return_value=False), \
             patch.object(handler, "record_leader_detection"):
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
            result = handler.lambda_handler(event, None)
            assert result["detected"] is True
            assert result["new_alert"] is True
            assert result["leader"]["msa_code"] == "26420"

    def test_no_threshold_crossed(self, handler):
        """When no metro crosses threshold, no detection."""
        event = {
            "disease": "influenza",
            "week": "202640",
            "metro_signals": {
                "26420": {"value": 0.5, "trend": "flat"},
                "19100": {"value": 0.3, "trend": "flat"},
            },
        }
        result = handler.lambda_handler(event, None)
        assert result["detected"] is False

    def test_multiple_metros_crossed(self, handler):
        """When multiple metros cross, highest value is primary leader."""
        with patch.object(handler, "check_existing_alert", return_value=False), \
             patch.object(handler, "record_leader_detection"):
            event = {
                "disease": "influenza",
                "week": "202650",
                "metro_signals": {
                    "26420": {"value": 4.5, "trend": "rising"},
                    "19100": {"value": 8.2, "trend": "rising"},
                    "12420": {"value": 2.1, "trend": "rising"},
                },
            }
            result = handler.lambda_handler(event, None)
            assert result["detected"] is True
            assert result["leader"]["msa_code"] == "19100"

    def test_duplicate_detection_suppressed(self, handler):
        """Already-alerted leader should not trigger new alert."""
        with patch.object(handler, "check_existing_alert", return_value=True):
            event = {
                "disease": "influenza",
                "week": "202646",
                "metro_signals": {
                    "26420": {"value": 5.0, "trend": "rising"},
                },
            }
            result = handler.lambda_handler(event, None)
            assert result["detected"] is True
            assert result["new_alert"] is False
