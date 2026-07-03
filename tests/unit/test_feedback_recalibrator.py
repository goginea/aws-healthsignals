"""Unit tests for feedback recalibrator Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal


mock_system_config = {
    "dynamodb_tables": {"calibration": "healthsignals-calibration-test"}
}


@pytest.fixture(scope="module")
def handler():
    from tests.conftest import load_handler
    return load_handler(
        "delivery/feedback_recalibrator",
        extra_patches={
            "shared.config_loader.get_system_config": mock_system_config,
            "boto3.resource": MagicMock(),
            "boto3.client": MagicMock(),
        },
    )


class TestDetermineSeasonFromWeek:
    """Test epiweek → season conversion."""

    def test_week_45_maps_to_current_fall(self, handler):
        assert handler.determine_season_from_week("202645") == "2026-27"

    def test_week_10_maps_to_previous_fall(self, handler):
        assert handler.determine_season_from_week("202710") == "2026-27"

    def test_week_30_maps_to_previous_season(self, handler):
        assert handler.determine_season_from_week("202630") == "2025-26"

    def test_malformed_week_returns_unknown(self, handler):
        assert handler.determine_season_from_week("bad_week") == "unknown"
        assert handler.determine_season_from_week("") == "unknown"


class TestCalculateAdjustment:
    """Test calibration adjustment calculation from feedback."""

    def test_on_time_feedback_no_adjustment(self, handler):
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
        ]
        result = handler.calculate_adjustment(records)
        assert result["lag_adjustment_weeks"] == 0.0
        assert result["severity_adjustment"] == 0.0

    def test_late_timing_increases_lag(self, handler):
        """All 'late' feedback → we underestimated lag → increase it."""
        records = [
            {"timing_accuracy": "late", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "late", "severity_accuracy": "about-right", "outbreak_occurred": True},
        ]
        result = handler.calculate_adjustment(records)
        assert result["lag_adjustment_weeks"] > 0

    def test_early_timing_decreases_lag(self, handler):
        """All 'early' feedback → we overestimated lag → decrease it."""
        records = [
            {"timing_accuracy": "early", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "early", "severity_accuracy": "about-right", "outbreak_occurred": True},
        ]
        result = handler.calculate_adjustment(records)
        assert result["lag_adjustment_weeks"] < 0

    def test_over_severity_decreases_multiplier(self, handler):
        """'over' severity → reduce multiplier."""
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "over", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "over", "outbreak_occurred": True},
        ]
        result = handler.calculate_adjustment(records)
        assert result["severity_adjustment"] < 0

    def test_under_severity_increases_multiplier(self, handler):
        """'under' severity → increase multiplier."""
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "under", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "under", "outbreak_occurred": True},
        ]
        result = handler.calculate_adjustment(records)
        assert result["severity_adjustment"] > 0

    def test_no_outbreak_negative_severity_signal(self, handler):
        """Outbreak didn't occur → strong negative severity signal."""
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "over", "outbreak_occurred": False},
        ]
        result = handler.calculate_adjustment(records)
        assert result["severity_adjustment"] < 0

    def test_accuracy_rate_calculated(self, handler):
        """accuracy_rate should be fraction of True outbreak_occurred."""
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": False},
        ]
        result = handler.calculate_adjustment(records)
        assert abs(result["accuracy_rate"] - 2 / 3) < 0.01

    def test_median_robust_to_outliers(self, handler):
        """Median-based adjustment should not be dominated by outliers."""
        records = [
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "on-time", "severity_accuracy": "about-right", "outbreak_occurred": True},
            {"timing_accuracy": "late", "severity_accuracy": "about-right", "outbreak_occurred": True},  # Outlier
        ]
        result = handler.calculate_adjustment(records)
        # Median of [0, 0, 1] = 0, not swayed by the single late
        assert result["lag_adjustment_weeks"] == 0.0


class TestApplyCalibrationUpdate:
    """Test blended calibration write to DynamoDB."""

    def test_blended_update_written(self, handler):
        """Update should blend 70% historical + 30% feedback."""
        with patch.object(handler, "calibration_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "lag_weeks": Decimal("4.0"),
                    "severity_multiplier": Decimal("2.0"),
                }
            }
            adjustment = {
                "lag_adjustment_weeks": 1.0,
                "severity_adjustment": 0.0,
                "feedback_count": 3,
                "accuracy_rate": 1.0,
            }
            handler.apply_calibration_update("48143:influenza:2026-27", adjustment)
            mock_table.put_item.assert_called_once()
            item = mock_table.put_item.call_args[1]["Item"]

        # new_lag = (0.7 * 4.0) + (0.3 * (4.0 + 1.0)) = 2.8 + 1.5 = 4.3
        assert float(item["lag_weeks"]) == pytest.approx(4.3, abs=0.01)

    def test_new_county_uses_defaults(self, handler):
        """No existing calibration should use hardcoded defaults."""
        with patch.object(handler, "calibration_table") as mock_table:
            mock_table.get_item.return_value = {}  # No existing item
            adjustment = {
                "lag_adjustment_weeks": 0.0,
                "severity_adjustment": 0.0,
                "feedback_count": 3,
                "accuracy_rate": 1.0,
            }
            handler.apply_calibration_update("99999:influenza:2026-27", adjustment)
            mock_table.put_item.assert_called_once()

    def test_lag_clamped_to_min(self, handler):
        """Lag should not go below 1 week."""
        with patch.object(handler, "calibration_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {"lag_weeks": Decimal("1.0"), "severity_multiplier": Decimal("2.0")}
            }
            adjustment = {
                "lag_adjustment_weeks": -10.0,
                "severity_adjustment": 0.0,
                "feedback_count": 3,
                "accuracy_rate": 0.5,
            }
            handler.apply_calibration_update("48143:influenza:2026-27", adjustment)
            item = mock_table.put_item.call_args[1]["Item"]

        assert float(item["lag_weeks"]) >= 1.0

    def test_lag_clamped_to_max(self, handler):
        """Lag should not exceed 12 weeks."""
        with patch.object(handler, "calibration_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {"lag_weeks": Decimal("11.0"), "severity_multiplier": Decimal("2.0")}
            }
            adjustment = {
                "lag_adjustment_weeks": 10.0,
                "severity_adjustment": 0.0,
                "feedback_count": 3,
                "accuracy_rate": 0.5,
            }
            handler.apply_calibration_update("48143:influenza:2026-27", adjustment)
            item = mock_table.put_item.call_args[1]["Item"]

        assert float(item["lag_weeks"]) <= 12.0

    def test_severity_clamped_to_min(self, handler):
        """Severity multiplier should not go below 0.5."""
        with patch.object(handler, "calibration_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {"lag_weeks": Decimal("4.0"), "severity_multiplier": Decimal("0.6")}
            }
            adjustment = {
                "lag_adjustment_weeks": 0.0,
                "severity_adjustment": -5.0,
                "feedback_count": 3,
                "accuracy_rate": 0.0,
            }
            handler.apply_calibration_update("48143:influenza:2026-27", adjustment)
            item = mock_table.put_item.call_args[1]["Item"]

        assert float(item["severity_multiplier"]) >= 0.5


class TestFullHandler:
    """End-to-end recalibrator handler."""

    def test_handler_processes_groups(self, handler):
        """Handler should process all feedback groups with enough responses."""
        with patch.object(handler, "feedback_table") as mock_feedback, \
             patch.object(handler, "apply_calibration_update") as mock_apply:
            mock_feedback.scan.return_value = {
                "Items": [
                    {"alert_id": "influenza_48143_202645", "county_fips": "48143",
                     "timing_accuracy": "on-time", "severity_accuracy": "about-right",
                     "outbreak_occurred": True},
                    {"alert_id": "influenza_48143_202645", "county_fips": "48143",
                     "timing_accuracy": "on-time", "severity_accuracy": "about-right",
                     "outbreak_occurred": True},
                    {"alert_id": "influenza_48143_202645", "county_fips": "48143",
                     "timing_accuracy": "on-time", "severity_accuracy": "about-right",
                     "outbreak_occurred": True},
                ]
            }
            result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["updated"]) == 1
        mock_apply.assert_called_once()

    def test_handler_skips_insufficient_feedback(self, handler):
        """Groups with fewer than MIN_FEEDBACK_COUNT should be skipped."""
        with patch.object(handler, "feedback_table") as mock_feedback, \
             patch.object(handler, "apply_calibration_update") as mock_apply:
            mock_feedback.scan.return_value = {
                "Items": [
                    {"alert_id": "influenza_48143_202645", "county_fips": "48143",
                     "timing_accuracy": "on-time", "outbreak_occurred": True},
                    {"alert_id": "influenza_48143_202645", "county_fips": "48143",
                     "timing_accuracy": "on-time", "outbreak_occurred": True},
                    # Only 2 — need 3
                ]
            }
            result = handler.lambda_handler({}, None)

        body = json.loads(result["body"])
        assert len(body["skipped"]) == 1
        mock_apply.assert_not_called()

    def test_handler_scan_failure_returns_error(self, handler):
        with patch.object(handler, "feedback_table") as mock_feedback:
            mock_feedback.scan.side_effect = Exception("DynamoDB down")
            result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 207
        body = json.loads(result["body"])
        assert len(body["errors"]) > 0
