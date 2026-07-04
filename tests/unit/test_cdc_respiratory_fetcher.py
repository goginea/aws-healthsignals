"""Unit tests for CDC Respiratory Activity fetcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {"infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}}
MOCK_NSSP_CONFIG = {
    "api": {
        "base_url": "https://data.cdc.gov/resource",
        "dataset_id": "rdmq-nq56",
        "app_token_env_var": "CDC_SOCRATA_APP_TOKEN",
        "timeout_seconds": 30,
        "max_records_per_query": 1000,
    },
    "query_defaults": {
        "lookback_days": 60,
        "visit_type_filter": "ed",
        "always_include_geographies": ["National"],
    },
    "s3_storage": {"prefix_pattern": "raw/cdc_nssp/{year}/W{week}/respiratory_activity.json"},
}
MOCK_STATES = [{"state_key": "texas", "state_name": "Texas", "cdc_geography_name": "Texas"}]
MOCK_DISEASES = [{"disease_key": "influenza", "data_sources": {"cdc_nssp": {"pathogen_name": "Influenza"}}}]


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "ingestion/cdc_respiratory_fetcher",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_data_source_config": MOCK_NSSP_CONFIG,
            "shared.config_loader.list_active_states": MOCK_STATES,
            "shared.config_loader.list_active_diseases": MOCK_DISEASES,
            "boto3.client": MagicMock(),
        },
    )


class TestRespiratoryFetcher:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_fetch_nssp_data_exists(self, handler):
        assert callable(handler.fetch_nssp_data)

    def test_handler_success(self, handler):
        mock_records = [{"geography": "Texas", "pathogen": "Influenza", "percent": "2.5", "week_end": "2026-06-21"}]
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
