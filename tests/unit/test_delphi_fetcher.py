"""Unit tests for Delphi Epidata API fetcher Lambda (config-driven version)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from tests.conftest import load_handler


# ── Mock configs ─────────────────────────────────────────────────────────
MOCK_SYSTEM = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}
}

MOCK_DELPHI_CONFIG = {
    "api": {"base_url": "https://api.delphi.cmu.edu/epidata/covidcast/", "timeout_seconds": 30},
    "query_defaults": {"lookback_weeks": 8, "geo_type": "msa"},
    "s3_storage": {"prefix_pattern": "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"},
}

MOCK_METROS = {
    "26420": {"short_name": "Houston", "state_abbreviation": "TX"},
    "19100": {"short_name": "DFW", "state_abbreviation": "TX"},
    "12420": {"short_name": "Austin", "state_abbreviation": "TX"},
    "41700": {"short_name": "San Antonio", "state_abbreviation": "TX"},
}

MOCK_DISEASES = [
    {"disease_key": "influenza", "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_influenza"}}},
    {"disease_key": "covid", "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_covid"}}},
    {"disease_key": "rsv", "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_rsv"}}},
]


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "ingestion/delphi_fetcher",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.get_data_source_config": MOCK_DELPHI_CONFIG,
            "shared.config_loader.get_all_sentinel_metros": MOCK_METROS,
            "shared.config_loader.list_active_diseases": MOCK_DISEASES,
            "boto3.client": MagicMock(),
        },
    )


class TestS3KeyBuilding:
    """Test S3 key partitioning logic."""

    def test_key_format(self, handler):
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        key = handler.build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)
        assert key.startswith("raw/delphi/nssp/pct_ed_visits_influenza/2026/")
        assert key.endswith("/26420.json")
        assert "/W" in key

    def test_key_different_signals(self, handler):
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        k1 = handler.build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)
        k2 = handler.build_s3_key(dt, "nssp", "pct_ed_visits_covid", "26420", pattern)
        assert k1 != k2
        assert "influenza" in k1
        assert "covid" in k2

    def test_key_different_metros(self, handler):
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        k1 = handler.build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)
        k2 = handler.build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "19100", pattern)
        assert "26420" in k1
        assert "19100" in k2


class TestFetchSignal:
    """Test API call construction."""

    def test_api_url_construction(self, handler):
        with patch.object(handler, "http") as mock_http:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.data = json.dumps({"epidata": [], "result": 1}).encode()
            mock_http.request.return_value = mock_response

            handler.fetch_signal("nssp", "pct_ed_visits_influenza", "26420", "20260601", "20260703")

            url = mock_http.request.call_args[0][1]
            assert "api.delphi.cmu.edu/epidata/covidcast" in url
            assert "data_source=nssp" in url
            assert "signal=pct_ed_visits_influenza" in url
            assert "geo_value=26420" in url

    def test_api_error_raises(self, handler):
        with patch.object(handler, "http") as mock_http:
            mock_response = MagicMock()
            mock_response.status = 500
            mock_response.data = b"Internal Server Error"
            mock_http.request.return_value = mock_response

            with pytest.raises(RuntimeError, match="500"):
                handler.fetch_signal("nssp", "pct_ed_visits_influenza", "26420", "20260601", "20260703")


class TestFullHandler:
    """Integration tests for the Lambda handler."""

    def test_handler_success(self, handler):
        with patch.object(handler, "fetch_signal", return_value={"epidata": [{"value": 1.5}], "result": 1}), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert len(body["fetched"]) > 0
            assert len(body["errors"]) == 0

    def test_handler_partial_failure(self, handler):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] % 3 == 0:
                raise RuntimeError("API timeout")
            return {"epidata": [{"value": 1.0}], "result": 1}

        with patch.object(handler, "fetch_signal", side_effect=side_effect), \
             patch.object(handler, "store_to_s3"):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] == 207
            body = json.loads(result["body"])
            assert len(body["fetched"]) > 0
            assert len(body["errors"]) > 0
