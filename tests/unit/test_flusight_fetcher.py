"""Unit tests for FluSight Forecast Fetcher Lambda."""
import os
import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import load_handler


MOCK_CSV = """reference_date,location,horizon,target,target_end_date,output_type,output_type_id,value
2026-05-30,48,1,wk inc flu hosp,2026-06-06,quantile,0.025,50
2026-05-30,48,1,wk inc flu hosp,2026-06-06,quantile,0.25,80
2026-05-30,48,1,wk inc flu hosp,2026-06-06,quantile,0.5,120
2026-05-30,48,1,wk inc flu hosp,2026-06-06,quantile,0.75,160
2026-05-30,48,1,wk inc flu hosp,2026-06-06,quantile,0.975,250
2026-05-30,48,2,wk inc flu hosp,2026-06-13,quantile,0.5,140
2026-05-30,US,1,wk inc flu hosp,2026-06-06,quantile,0.5,5000
"""


@pytest.fixture(scope="module")
def handler():
    os.environ["DATA_BUCKET"] = "test-bucket"
    os.environ["FORECAST_STATE_TABLE"] = "test-forecast-state"

    return load_handler(
        "ingestion/flusight_forecast_fetcher",
        extra_patches={
            "shared.config_loader.get_data_source_config": {},
            "boto3.client": MagicMock(),
            "boto3.resource": MagicMock(),
        },
    )


class TestCSVParsing:
    def test_parse_flusight_csv_extracts_forecasts(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        # Should have 2 locations: TX (48) and US
        assert len(forecasts) == 2

    def test_texas_forecast_has_correct_provider(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        tx = next(f for f in forecasts if f["geo_value"] == "TX")
        assert tx["provider"] == "cdc_flusight"
        assert tx["disease"] == "influenza"

    def test_texas_has_two_horizons(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        tx = next(f for f in forecasts if f["geo_value"] == "TX")
        assert len(tx["predictions"]) == 2

    def test_point_estimate_from_median(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        tx = next(f for f in forecasts if f["geo_value"] == "TX")
        h1 = next(p for p in tx["predictions"] if p["horizon_weeks"] == 1)
        assert h1["point_estimate"] == 120.0

    def test_quantiles_extracted(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        tx = next(f for f in forecasts if f["geo_value"] == "TX")
        h1 = next(p for p in tx["predictions"] if p["horizon_weeks"] == 1)
        assert "0.025" in h1["quantiles"]
        assert h1["quantiles"]["0.025"] == 50.0

    def test_national_forecast_geo_level(self, handler):
        forecasts = handler.parse_flusight_csv(MOCK_CSV, "2026-05-30")
        us = next(f for f in forecasts if f["geo_value"] == "US")
        assert us["geo_level"] == "national"

    def test_empty_csv_returns_empty(self, handler):
        forecasts = handler.parse_flusight_csv("", "2026-05-30")
        assert forecasts == []


class TestDateToISOWeek:
    def test_converts_correctly(self, handler):
        assert handler.date_to_iso_week("2026-11-15") == "2026-W46"

    def test_beginning_of_year(self, handler):
        result = handler.date_to_iso_week("2026-01-05")
        assert "W" in result


class TestOutbreakIdGeneration:
    def test_find_latest_reference_date_returns_wednesday(self, handler):
        from datetime import datetime
        result = handler.find_latest_reference_date()
        d = datetime.strptime(result, "%Y-%m-%d")
        assert d.weekday() == 2  # Wednesday
