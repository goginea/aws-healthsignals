"""Unit tests for CDC Respiratory Activity fetcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {"infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}}
MOCK_NSSP_CONFIG = {
    "api": {
        "base_url": "https://data.cdc.gov/resource",
        "dataset_id": "vutn-jzwm",
        "app_token_env_var": "CDC_SOCRATA_APP_TOKEN",
        "timeout_seconds": 30,
        "max_records_per_query": 1000,
    },
    "query_defaults": {
        "lookback_days": 60,
        "always_include_geographies": ["United States"],
    },
    "key_fields": {
        "geography": "geography",
        "pathogen": "pathogen",
        "week_end": "week_end",
        "percent": "percent_visits",
    },
    "s3_storage": {"prefix_pattern": "raw/cdc_nssp/{year}/W{week}/respiratory_activity.json"},
}
MOCK_STATES = [
    {
        "state_key": "texas",
        "state_name": "Texas",
        "cdc_geography_name": "Texas",
        "sentinel_metros": {
            "26420": {
                "county_fips": ["48201", "48157"],
                "county_names": ["Harris", "Fort Bend"],
            }
        },
        "subscribing_counties": [
            {"county_fips": "48143", "county_name": "Erath County"}
        ],
    }
]
MOCK_DISEASES = [{"disease_key": "influenza", "data_sources": {"cdc_nssp": {"pathogen_name": "Influenza"}}}]

MOCK_COUNTY_CONFIG = {
    "enabled": True,
    "priority": "supplemental",
    "api": {
        "base_url": "https://data.cdc.gov/resource",
        "dataset_id": "rdmq-nq56",
        "app_token_env_var": "CDC_SOCRATA_APP_TOKEN",
        "timeout_seconds": 30,
        "max_records_per_query": 1000,
    },
    "query_defaults": {
        "lookback_days": 60,
        "fips_field": "fips",
        "week_end_field": "week_end",
    },
    "wide_format_fields": {
        "influenza": {
            "percent": "percent_visits_influenza",
            "percent_smoothed": "percent_visits_smoothed_1",
            "trend": "ed_trends_influenza",
        }
    },
    "trend_mapping": {
        "Increasing": "rising",
        "Decreasing": "declining",
        "No Change": "stable",
        "Data Unavailable": "unknown",
    },
    "s3_storage": {"prefix_pattern": "raw/cdc_nssp_county/{disease}/{year}/W{week}/{fips}.json"},
}


def _get_data_source_config(name):
    return MOCK_COUNTY_CONFIG if name == "cdc_nssp_county" else MOCK_NSSP_CONFIG


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "ingestion/cdc_respiratory_fetcher",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_data_source_config": MagicMock(side_effect=_get_data_source_config),
            "shared.config_loader.list_active_states": MOCK_STATES,
            "shared.config_loader.list_active_diseases": MOCK_DISEASES,
            "shared.config_loader.get_all_sentinel_metros": {},
            "shared.config_loader.get_subscribing_counties": [],
            "boto3.client": MagicMock(),
        },
    )


SAMPLE_COUNTY_ROW = {
    "week_end": "2026-09-05T00:00:00.000",
    "geography": "Texas",
    "county": "Harris",
    "fips": "48201",
    "percent_visits_influenza": "0.31",
    "percent_visits_smoothed_1": "0.32",
    "ed_trends_influenza": "Increasing",
}


class TestRespiratoryFetcher:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_fetch_nssp_data_exists(self, handler):
        assert callable(handler.fetch_nssp_data)

    def test_handler_success(self, handler):
        mock_records = [{"geography": "Texas", "pathogen": "Influenza", "percent_visits": "2.5", "week_end": "2026-06-21"}]
        with patch.object(handler, "fetch_nssp_data", return_value=mock_records), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] in (200, 207)

    def test_handler_api_error(self, handler):
        with patch.object(handler, "fetch_nssp_data", side_effect=RuntimeError("429")), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            body = json.loads(result["body"])
            assert len(body.get("errors", [])) > 0


class TestCountyLevelIngestion:
    def test_to_float_parses_socrata_strings(self, handler):
        assert handler._to_float("0.31") == 0.31
        assert handler._to_float(1.2) == 1.2
        assert handler._to_float("") is None
        assert handler._to_float(None) is None
        assert handler._to_float("N/A") is None

    def test_collect_target_counties_includes_metro_and_subscribing(self, handler):
        targets = handler._collect_target_counties(MOCK_STATES)
        # Metro counties + subscribing county all present
        assert "48201" in targets  # Harris (metro)
        assert "48157" in targets  # Fort Bend (metro)
        assert "48143" in targets  # Erath (subscribing)
        assert targets["48201"]["county_name"] == "Harris"
        assert "metro" in targets["48201"]["roles"]
        assert "subscribing" in targets["48143"]["roles"]
        assert targets["48201"]["state_key"] == "texas"

    def test_parse_county_row_maps_wide_format(self, handler):
        field_map = MOCK_COUNTY_CONFIG["wide_format_fields"]["influenza"]
        trend_mapping = MOCK_COUNTY_CONFIG["trend_mapping"]
        meta = {"county_name": "Harris", "state_key": "texas", "roles": {"metro"}}
        record = handler.parse_county_row(
            row=SAMPLE_COUNTY_ROW,
            disease_key="influenza",
            field_map=field_map,
            trend_mapping=trend_mapping,
            meta=meta,
            fips="48201",
        )
        assert record["value"] == 0.31
        assert record["smoothed_value"] == 0.32
        assert record["trend"] == "rising"  # "Increasing" -> "rising"
        assert record["trend_raw"] == "Increasing"
        assert record["disease"] == "influenza"
        assert record["fips"] == "48201"
        assert record["source"] == "cdc_nssp_county"

    def test_parse_county_row_returns_none_when_value_missing(self, handler):
        field_map = MOCK_COUNTY_CONFIG["wide_format_fields"]["influenza"]
        trend_mapping = MOCK_COUNTY_CONFIG["trend_mapping"]
        row = {"fips": "48201", "ed_trends_influenza": "Data Unavailable"}  # no percent
        record = handler.parse_county_row(
            row=row,
            disease_key="influenza",
            field_map=field_map,
            trend_mapping=trend_mapping,
            meta={"roles": set()},
            fips="48201",
        )
        assert record is None

    def test_parse_county_row_data_unavailable_trend(self, handler):
        field_map = MOCK_COUNTY_CONFIG["wide_format_fields"]["influenza"]
        trend_mapping = MOCK_COUNTY_CONFIG["trend_mapping"]
        row = {**SAMPLE_COUNTY_ROW, "ed_trends_influenza": "Data Unavailable"}
        record = handler.parse_county_row(
            row=row,
            disease_key="influenza",
            field_map=field_map,
            trend_mapping=trend_mapping,
            meta={"roles": set()},
            fips="48201",
        )
        # value still present, but trend maps to unknown
        assert record["value"] == 0.31
        assert record["trend"] == "unknown"

    def test_year_week_from_week_end(self, handler):
        from datetime import datetime
        year, week = handler._year_week_from_week_end("2026-09-05T00:00:00.000", datetime.utcnow())
        assert year == "2026"
        assert week == "35"  # ISO %W week for 2026-09-05; 'W' prefix is in the S3 pattern

    def test_fetch_county_level_data_writes_per_county(self, handler):
        from datetime import datetime
        with patch.object(handler, "fetch_county_row", return_value=SAMPLE_COUNTY_ROW), \
             patch.object(handler, "store_to_s3") as mock_store:
            summary = handler.fetch_county_level_data(
                active_states=MOCK_STATES,
                active_diseases=MOCK_DISEASES,
                data_bucket="test-bucket",
                app_token="",
                today=datetime.utcnow(),
            )
            # 3 target counties (Harris, Fort Bend, Erath) x 1 disease = 3 writes
            assert summary["counties_targeted"] == 3
            assert len(summary["written"]) == 3
            assert mock_store.call_count == 3

    def test_fetch_county_level_data_skips_when_disabled(self, handler):
        from datetime import datetime
        disabled = {**MOCK_COUNTY_CONFIG, "enabled": False}
        with patch.object(handler, "get_data_source_config", return_value=disabled):
            summary = handler.fetch_county_level_data(
                active_states=MOCK_STATES,
                active_diseases=MOCK_DISEASES,
                data_bucket="test-bucket",
                app_token="",
                today=datetime.utcnow(),
            )
            assert summary.get("skipped") is True

    def test_fetch_county_level_data_no_data(self, handler):
        from datetime import datetime
        with patch.object(handler, "fetch_county_row", return_value={}), \
             patch.object(handler, "store_to_s3") as mock_store:
            summary = handler.fetch_county_level_data(
                active_states=MOCK_STATES,
                active_diseases=MOCK_DISEASES,
                data_bucket="test-bucket",
                app_token="",
                today=datetime.utcnow(),
            )
            assert len(summary["no_data"]) == 3
            assert mock_store.call_count == 0
