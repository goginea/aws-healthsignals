"""Unit tests for timing estimation Lambda (config-driven version)."""
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

from tests.conftest import load_handler


# ── Mock configs ─────────────────────────────────────────────────────────
MOCK_SYSTEM = {
    "dynamodb_tables": {"calibration": "healthsignals-calibration-test"}
}

MOCK_DISEASE = {
    "prediction": {
        "typical_lag_range_weeks": [3, 6],
        "severity_multiplier_typical_range": [1.0, 3.0],
        "confidence_degrades_after_seasons": 2,
    }
}


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "prediction/timing_estimation",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_disease_config": MOCK_DISEASE,
            "boto3.resource": MagicMock(),
        },
    )


class TestLagEstimate:
    """Test historical calibration-based lag calculations."""

    def test_three_season_median(self, handler):
        """With 3 seasons of data, use median lag."""
        calibration = [
            {"lag_weeks": Decimal("4")},
            {"lag_weeks": Decimal("5")},
            {"lag_weeks": Decimal("4")},
        ]
        result = handler.calculate_lag_estimate(calibration)
        assert result["median"] == 4.0

    def test_single_season(self, handler):
        """Single season returns that value as median."""
        calibration = [{"lag_weeks": Decimal("6")}]
        result = handler.calculate_lag_estimate(calibration)
        assert result["median"] == 6.0

    def test_empty_calibration(self, handler):
        """Empty calibration returns None."""
        result = handler.calculate_lag_estimate([])
        # Handler returns defaults when no data: {"median": 4.0, "min": 3.0, "max": 6.0}
        assert result["median"] == 4.0


class TestSeverityMultiplier:
    """Test severity multiplier calculation."""

    def test_three_season_severity(self, handler):
        """Median severity multiplier from 3 seasons."""
        calibration = [
            {"severity_multiplier": Decimal("1.8")},
            {"severity_multiplier": Decimal("2.1")},
            {"severity_multiplier": Decimal("1.6")},
        ]
        result = handler.calculate_severity_multiplier(calibration)
        assert result["median"] == 1.8

    def test_single_season_severity(self, handler):
        """Single season returns that value."""
        calibration = [{"severity_multiplier": Decimal("2.5")}]
        result = handler.calculate_severity_multiplier(calibration)
        assert result["median"] == 2.5


class TestConfidence:
    """Test confidence score calculation."""

    def test_three_seasons_high_confidence(self, handler):
        """3+ seasons should give higher confidence."""
        calibration = [
            {"lag_weeks": Decimal("4")},
            {"lag_weeks": Decimal("4")},
            {"lag_weeks": Decimal("5")},
        ]
        conf = handler.calculate_confidence(calibration, degrades_after=2)
        assert 0.6 <= conf <= 0.9

    def test_single_season_low_confidence(self, handler):
        """1 season should give lower confidence."""
        calibration = [{"lag_weeks": Decimal("5")}]
        conf = handler.calculate_confidence(calibration, degrades_after=2)
        assert conf <= 0.6

    def test_zero_seasons(self, handler):
        """No calibration data → minimal confidence."""
        conf = handler.calculate_confidence([], degrades_after=2)
        assert conf <= 0.3


class TestFullHandler:
    """Integration tests for the timing estimation handler."""

    def test_handler_with_calibration(self, handler):
        """Handler should use calibration data when available."""
        mock_data = [
            {"lag_weeks": Decimal("4"), "severity_multiplier": Decimal("2.0")},
            {"lag_weeks": Decimal("5"), "severity_multiplier": Decimal("1.8")},
        ]
        with patch.object(handler, "get_calibration_data", return_value=mock_data):
            event = {
                "leader_msa": "26420",
                "disease": "influenza",
                "week": "202645",
                "affected_counties": [
                    {
                        "county_fips": "48143",
                        "county_name": "Erath County",
                        "expected_lag_weeks": 4.5,
                        "alert_contacts": [{"type": "email", "value": "test@test.com"}],
                    }
                ],
            }
            result = handler.lambda_handler(event, None)
            assert result["total_counties"] == 1
            assert result["estimates"][0]["county_name"] == "Erath County"

    def test_handler_no_counties(self, handler):
        """Handler should handle empty county list."""
        event = {
            "leader_msa": "26420",
            "disease": "influenza",
            "week": "202645",
            "affected_counties": [],
        }
        result = handler.lambda_handler(event, None)
        assert result["estimates"] == []
