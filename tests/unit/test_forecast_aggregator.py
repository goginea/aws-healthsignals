"""Unit tests for Forecast Aggregator Lambda."""
import os
import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import load_handler


MOCK_SYSTEM = {
    "forecast_providers": {
        "enabled": True,
        "aggregation_method": "weighted_mean",
        "conflict_threshold_pct": 50,
        "forecast_state_table": "test-forecast-state",
        "max_providers": 10,
    },
    "infrastructure": {"data_bucket_name_pattern": "test-bucket"},
}


@pytest.fixture(scope="module")
def handler():
    os.environ["FORECAST_STATE_TABLE"] = "test-forecast-state"
    os.environ["EVENT_BUS_NAME"] = "default"

    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    return load_handler(
        "prediction/forecast_aggregator",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "boto3.resource": MagicMock(return_value=mock_dynamo),
            "boto3.client": MagicMock(),
        },
    )


def _make_forecast(provider="cdc_flusight", point=1200, weight=1.0):
    return {
        "provider": provider,
        "disease": "influenza",
        "geo_level": "state",
        "geo_value": "texas",
        "target": "hospitalizations",
        "trust_weight": weight,
        "predictions": [
            {"horizon_weeks": 1, "point_estimate": point, "quantiles": {"0.25": point * 0.8, "0.75": point * 1.2}},
            {"horizon_weeks": 2, "point_estimate": point * 1.1},
        ],
    }


class TestAggregation:
    def test_single_provider_no_aggregation(self, handler):
        forecasts = [_make_forecast()]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        assert result["provider_count"] == 1
        assert result["agreement_status"] == "single_source"
        assert result["conflict"] is False

    def test_two_agreeing_providers(self, handler):
        forecasts = [
            _make_forecast("cdc_flusight", 1200, 1.0),
            _make_forecast("custom_model", 1300, 0.7),
        ]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        assert result["provider_count"] == 2
        assert result["agreement_status"] == "consensus"
        assert result["conflict"] is False

    def test_weighted_mean_calculation(self, handler):
        forecasts = [
            _make_forecast("provider_a", 1000, 1.0),
            _make_forecast("provider_b", 2000, 1.0),
        ]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        # Equal weights: (1000*1.0 + 2000*1.0) / 2.0 = 1500
        h1 = result["predictions"][0]
        assert h1["point_estimate"] == 1500.0

    def test_conflict_detected_when_large_disagreement(self, handler):
        forecasts = [
            _make_forecast("provider_a", 1000, 1.0),
            _make_forecast("provider_b", 5000, 1.0),  # 400% difference
        ]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        assert result["conflict"] is True
        assert result["agreement_status"] == "disagreement"

    def test_no_conflict_within_threshold(self, handler):
        forecasts = [
            _make_forecast("provider_a", 1000, 1.0),
            _make_forecast("provider_b", 1200, 1.0),  # 20% difference (< 50%)
        ]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        assert result["conflict"] is False

    def test_quantile_blending(self, handler):
        forecasts = [
            _make_forecast("provider_a", 1000, 1.0),
            _make_forecast("provider_b", 2000, 1.0),
        ]
        result = handler.aggregate_forecasts(forecasts, "texas", "influenza", "2026-W45")
        h1 = result["predictions"][0]
        # Quantile 0.25: (800*1.0 + 1600*1.0) / 2 = 1200
        assert "quantiles" in h1
        assert h1["quantiles"]["0.25"] == 1200.0
