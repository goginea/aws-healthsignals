"""Unit tests for CDC Wastewater fetcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {"infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}}
MOCK_WW_CONFIG = {
    "api": {
        "base_url": "https://data.cdc.gov/resource",
        "app_token_env_var": "CDC_SOCRATA_APP_TOKEN",
        "timeout_seconds": 30,
        "pagination_limit": 10000,
        "max_records": 100000,
    },
    "query_defaults": {
        "lookback_days": 30,
        "state_field": "state_territory",
        "date_field": "week_end",
        "pathogen_field": "pathogen_target",
        "county_names_field": "counties_served",
    },
    "s3_storage": {"prefix_pattern": "raw/cdc_wastewater/{disease}/{year}/W{week}/data.json"},
}
MOCK_STATES = [{
    "state_key": "texas", "state_name": "Texas", "state_abbreviation": "TX",
    "sentinel_metros": {"26420": {"county_names": ["Harris"], "county_fips": ["48201"], "short_name": "Houston"}},
}]
MOCK_DISEASES = [{"disease_key": "influenza", "data_sources": {"cdc_wastewater": {"socrata_dataset_id": "atcp-73re", "pathogen_target": "Influenza A virus"}}}]


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "ingestion/cdc_wastewater_fetcher",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_data_source_config": MOCK_WW_CONFIG,
            "shared.config_loader.list_active_states": MOCK_STATES,
            "shared.config_loader.list_active_diseases": MOCK_DISEASES,
            "boto3.client": MagicMock(),
        },
    )


class TestWastewaterFetcher:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_fetch_wastewater_data_exists(self, handler):
        assert callable(handler.fetch_wastewater_data)

    def test_filter_to_metro_counties_exists(self, handler):
        assert callable(handler.filter_to_metro_counties)

    def test_handler_success(self, handler):
        mock_records = [{"counties_served": "Harris", "site_wval": "1.9", "week_end": "2026-08-29"}]
        with patch.object(handler, "fetch_wastewater_data", return_value=mock_records), \
             patch.object(handler, "filter_to_metro_counties", return_value=mock_records), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] in (200, 207)

    def test_filter_includes_matching_county_name(self, handler):
        records = [
            {"counties_served": "Harris", "site_wval": "3.2"},
            {"counties_served": "Nowhere", "site_wval": "1.0"},
        ]
        # New signature: filter_to_metro_counties(records, metro_name_map, county_names_field)
        # metro_name_map keys are lowercased county names.
        filtered = handler.filter_to_metro_counties(records, {"harris": "Houston"}, "counties_served")
        assert isinstance(filtered, list)
        assert len(filtered) == 1
        assert filtered[0]["counties_served"] == "Harris"
        assert filtered[0]["_matched_metro"] == "Houston"

    def test_handler_api_error(self, handler):
        with patch.object(handler, "fetch_wastewater_data", side_effect=RuntimeError("429 rate limit")), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            body = json.loads(result["body"])
            assert len(body.get("errors", [])) > 0
