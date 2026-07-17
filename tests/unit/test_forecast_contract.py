"""Unit tests for shared forecast_contract.py validation and normalization."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambdas", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambdas"))

from shared.forecast_contract import validate_forecast, normalize_forecast_geo, build_dynamodb_key


def _valid_forecast():
    return {
        "provider": "cdc_flusight",
        "disease": "influenza",
        "geo_level": "state",
        "geo_value": "TX",
        "forecast_date": "2026-11-15",
        "target": "hospitalizations",
        "predictions": [
            {"horizon_weeks": 1, "point_estimate": 1200, "quantiles": {"0.025": 800, "0.975": 1800}},
            {"horizon_weeks": 2, "point_estimate": 1500},
        ],
    }


class TestValidateForecast:
    def test_valid_forecast_passes(self):
        assert validate_forecast(_valid_forecast()) is True

    def test_missing_provider_fails(self):
        f = _valid_forecast()
        del f["provider"]
        assert validate_forecast(f) is False

    def test_missing_predictions_fails(self):
        f = _valid_forecast()
        del f["predictions"]
        assert validate_forecast(f) is False

    def test_empty_predictions_fails(self):
        f = _valid_forecast()
        f["predictions"] = []
        assert validate_forecast(f) is False

    def test_invalid_geo_level_fails(self):
        f = _valid_forecast()
        f["geo_level"] = "county"
        assert validate_forecast(f) is False

    def test_invalid_target_fails(self):
        f = _valid_forecast()
        f["target"] = "deaths"
        assert validate_forecast(f) is False

    def test_missing_horizon_weeks_fails(self):
        f = _valid_forecast()
        del f["predictions"][0]["horizon_weeks"]
        assert validate_forecast(f) is False

    def test_missing_point_estimate_fails(self):
        f = _valid_forecast()
        del f["predictions"][0]["point_estimate"]
        assert validate_forecast(f) is False

    def test_negative_horizon_fails(self):
        f = _valid_forecast()
        f["predictions"][0]["horizon_weeks"] = -1
        assert validate_forecast(f) is False

    def test_non_numeric_point_estimate_fails(self):
        f = _valid_forecast()
        f["predictions"][0]["point_estimate"] = "high"
        assert validate_forecast(f) is False

    def test_national_geo_level_valid(self):
        f = _valid_forecast()
        f["geo_level"] = "national"
        f["geo_value"] = "US"
        assert validate_forecast(f) is True


class TestNormalizeForecastGeo:
    def test_state_abbreviation_normalized(self):
        f = _valid_forecast()
        result = normalize_forecast_geo(f)
        assert result["geo_value"] == "texas"

    def test_national_stays_as_us(self):
        f = _valid_forecast()
        f["geo_level"] = "national"
        f["geo_value"] = "US"
        result = normalize_forecast_geo(f)
        assert result["geo_value"] == "US"

    def test_full_state_name_normalized(self):
        f = _valid_forecast()
        f["geo_value"] = "North Carolina"
        result = normalize_forecast_geo(f)
        assert result["geo_value"] == "north carolina"

    def test_unrecognized_state_kept_as_is(self):
        f = _valid_forecast()
        f["geo_value"] = "UNKNOWN"
        result = normalize_forecast_geo(f)
        assert result["geo_value"] == "UNKNOWN"


class TestBuildDynamoDBKey:
    def test_builds_correct_key(self):
        key = build_dynamodb_key("texas", "influenza", "2026-W45")
        assert key == {"geo_key": "texas", "disease_week": "influenza_2026-W45"}
