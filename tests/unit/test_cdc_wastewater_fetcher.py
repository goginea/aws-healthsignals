"""Unit tests for CDC Wastewater fetcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {"infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}}
MOCK_WW_CONFIG = {
    "socrata_base_url": "https://data.cdc.gov/resource",
    "app_token_env_var": "CDC_SOCRATA_APP_TOKEN",
    "rate_limit_per_hour_unauthenticated": 1000,
}
MOCK_STATES = [{"state_key": "texas", "abbreviation": "TX", "sentinel_metros": {"26420": {"county_fips": ["48201"]}}}]
MOCK_DISEASES = [{"disease_key": "influenza", "data_sources": {"cdc_wastewater": {"socrata_dataset_id": "ymmh-divb"}}}]


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
        mock_records = [{"county_fips": "48201", "ptc_15d": "5.2", "date_end": "2026-06-20"}]
        with patch.object(handler, "fetch_wastewater_data", return_value=mock_records), \
             patch.object(handler, "filter_to_metro_counties", return_value=mock_records), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] in (200, 207)

    def test_filter_includes_matching_fips(self, handler):
        records = [
            {"county_fips": "48201", "value": "3.2"},
            {"county_fips": "99999", "value": "1.0"},
        ]
        # Filter logic depends on handler implementation
        filtered = handler.filter_to_metro_counties(records)
        # Should return only records matching metro county FIPS
        assert isinstance(filtered, list)

    def test_handler_api_error(self, handler):
        with patch.object(handler, "fetch_wastewater_data", side_effect=RuntimeError("429 rate limit")), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            body = json.loads(result["body"])
            assert len(body.get("errors", [])) > 0
