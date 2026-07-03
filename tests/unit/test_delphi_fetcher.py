"""Unit tests for Delphi Epidata API fetcher Lambda (config-driven version)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

import sys
sys.path.insert(0, "lambdas/ingestion/delphi_fetcher")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")


# Mock the config_loader before importing handler
mock_system_config = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"}
}

mock_delphi_config = {
    "api": {"base_url": "https://api.delphi.cmu.edu/epidata/covidcast/", "timeout_seconds": 30},
    "query_defaults": {"lookback_weeks": 8, "geo_type": "msa"},
    "s3_storage": {"prefix_pattern": "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"},
}

mock_metros = {
    "26420": {"short_name": "Houston", "state_abbreviation": "TX"},
    "19100": {"short_name": "DFW", "state_abbreviation": "TX"},
    "12420": {"short_name": "Austin", "state_abbreviation": "TX"},
    "41700": {"short_name": "San Antonio", "state_abbreviation": "TX"},
}

mock_diseases = [
    {
        "disease_key": "influenza",
        "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_influenza"}},
    },
    {
        "disease_key": "covid",
        "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_covid"}},
    },
    {
        "disease_key": "rsv",
        "data_sources": {"delphi": {"data_source": "nssp", "signal": "pct_ed_visits_rsv"}},
    },
]


@pytest.fixture(autouse=True)
def mock_config():
    """Mock config_loader for all tests."""
    with patch("handler.get_system_config", return_value=mock_system_config), \
         patch("handler.get_data_source_config", return_value=mock_delphi_config), \
         patch("handler.get_all_sentinel_metros", return_value=mock_metros), \
         patch("handler.list_active_diseases", return_value=mock_diseases):
        yield


from handler import lambda_handler, fetch_signal, build_s3_key, store_to_s3


class TestS3KeyBuilding:
    """Test S3 key partitioning logic."""

    def test_key_format(self):
        """S3 key should follow expected partition pattern."""
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        key = build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)

        assert key.startswith("raw/delphi/nssp/pct_ed_visits_influenza/2026/")
        assert key.endswith("/26420.json")
        assert "/W" in key

    def test_key_different_signals(self):
        """Different signals should produce different keys."""
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        key_flu = build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)
        key_covid = build_s3_key(dt, "nssp", "pct_ed_visits_covid", "26420", pattern)

        assert key_flu != key_covid
        assert "influenza" in key_flu
        assert "covid" in key_covid

    def test_key_different_metros(self):
        """Different metros should produce different keys."""
        dt = datetime(2026, 7, 3)
        pattern = "raw/delphi/{data_source}/{signal}/{year}/W{week}/{geo_value}.json"
        key_houston = build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "26420", pattern)
        key_dfw = build_s3_key(dt, "nssp", "pct_ed_visits_influenza", "19100", pattern)

        assert "26420" in key_houston
        assert "19100" in key_dfw


class TestFetchSignal:
    """Test API call construction."""

    @patch("handler.http")
    def test_api_url_construction(self, mock_http):
        """API call should use correct base URL and parameters."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"epidata": [], "result": 1}).encode()
        mock_http.request.return_value = mock_response

        fetch_signal(
            api_base="https://api.delphi.cmu.edu/epidata/covidcast/",
            data_source="nssp",
            signal_name="pct_ed_visits_influenza",
            geo_type="msa",
            geo_value="26420",
            start_date="20260601",
            end_date="20260703",
        )

        call_args = mock_http.request.call_args
        url = call_args[0][1]

        assert "api.delphi.cmu.edu/epidata/covidcast" in url
        assert "data_source=nssp" in url
        assert "signal=pct_ed_visits_influenza" in url
        assert "geo_type=msa" in url
        assert "geo_value=26420" in url
        assert "time_type=day" in url

    @patch("handler.http")
    def test_api_error_raises(self, mock_http):
        """Non-200 response should raise RuntimeError."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.data = b"Internal Server Error"
        mock_http.request.return_value = mock_response

        with pytest.raises(RuntimeError, match="Delphi API returned 500"):
            fetch_signal(
                api_base="https://api.delphi.cmu.edu/epidata/covidcast/",
                data_source="nssp",
                signal_name="pct_ed_visits_influenza",
                geo_type="msa",
                geo_value="26420",
                start_date="20260601",
                end_date="20260703",
            )


class TestFullHandler:
    """Integration tests for the Lambda handler."""

    @patch("handler.store_to_s3")
    @patch("handler.fetch_signal")
    def test_handler_success(self, mock_fetch, mock_store):
        """Successful fetch should store to S3 and return 200."""
        mock_fetch.return_value = {"epidata": [{"value": 1.5}], "result": 1}

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["fetched"]) > 0
        assert len(body["errors"]) == 0

    @patch("handler.store_to_s3")
    @patch("handler.fetch_signal")
    def test_handler_partial_failure(self, mock_fetch, mock_store):
        """Partial failure should return 207 with both fetched and errors."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] % 3 == 0:
                raise RuntimeError("API timeout")
            return {"epidata": [{"value": 1.0}], "result": 1}

        mock_fetch.side_effect = side_effect

        result = lambda_handler({}, None)

        assert result["statusCode"] == 207
        body = json.loads(result["body"])
        assert len(body["fetched"]) > 0
        assert len(body["errors"]) > 0

    @patch("handler.store_to_s3")
    @patch("handler.fetch_signal")
    def test_handler_fetches_all_metro_disease_combos(self, mock_fetch, mock_store):
        """Handler should fetch for all metros × all diseases."""
        mock_fetch.return_value = {"epidata": [{"value": 1.0}], "result": 1}

        result = lambda_handler({}, None)

        body = json.loads(result["body"])
        # 4 metros × 3 diseases = 12 combinations
        assert len(body["fetched"]) == 12
