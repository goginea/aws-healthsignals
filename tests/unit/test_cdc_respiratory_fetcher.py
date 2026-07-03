"""Unit tests for CDC NSSP Respiratory Activity Fetcher."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas/ingestion/cdc_respiratory_fetcher"))


class TestFetchNsspData:
    """Test the Socrata API query construction and parsing."""

    @patch("handler.http")
    @patch("handler.get_system_config")
    @patch("handler.get_data_source_config")
    @patch("handler.list_active_states")
    @patch("handler.list_active_diseases")
    def test_soql_query_construction(
        self, mock_diseases, mock_states, mock_ds_config, mock_sys_config, mock_http
    ):
        """SoQL WHERE clause should include geography, pathogen, visit_type, and date."""
        from handler import fetch_nssp_data

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps([{"week_end": "2026-06-28", "percent": "2.5"}]).encode()
        mock_http.request.return_value = mock_response

        fetch_nssp_data(
            api_base="https://data.cdc.gov/resource",
            dataset_id="rdmq-nq56",
            geography="Texas",
            pathogen="Influenza",
            visit_type="ed",
            date_after="2026-05-01",
            app_token="",
            timeout=30,
            max_records=1000,
        )

        call_args = mock_http.request.call_args
        url = call_args[0][1]

        assert "rdmq-nq56.json" in url
        assert "geography%3D%27Texas%27" in url or "geography='Texas'" in url.replace("%27", "'")
        assert "pathogen" in url
        assert "visit_type" in url
        assert "week_end" in url

    @patch("handler.http")
    def test_rate_limit_429_raises(self, mock_http):
        """429 response should raise RuntimeError."""
        from handler import fetch_nssp_data

        mock_response = MagicMock()
        mock_response.status = 429
        mock_response.data = b"Rate limited"
        mock_http.request.return_value = mock_response

        with pytest.raises(RuntimeError, match="rate limit"):
            fetch_nssp_data(
                api_base="https://data.cdc.gov/resource",
                dataset_id="rdmq-nq56",
                geography="Texas",
                pathogen="Influenza",
                visit_type="ed",
                date_after="2026-05-01",
            )

    @patch("handler.http")
    def test_server_error_raises(self, mock_http):
        """Non-200/429 response should raise RuntimeError with status."""
        from handler import fetch_nssp_data

        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.data = b"Internal Server Error"
        mock_http.request.return_value = mock_response

        with pytest.raises(RuntimeError, match="500"):
            fetch_nssp_data(
                api_base="https://data.cdc.gov/resource",
                dataset_id="rdmq-nq56",
                geography="Texas",
                pathogen="Influenza",
                visit_type="ed",
                date_after="2026-05-01",
            )

    @patch("handler.http")
    def test_app_token_sent_as_header(self, mock_http):
        """When app_token is provided, it should be sent as X-App-Token header."""
        from handler import fetch_nssp_data

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps([]).encode()
        mock_http.request.return_value = mock_response

        fetch_nssp_data(
            api_base="https://data.cdc.gov/resource",
            dataset_id="rdmq-nq56",
            geography="Texas",
            pathogen="Influenza",
            visit_type="ed",
            date_after="2026-05-01",
            app_token="my-secret-token",
        )

        call_kwargs = mock_http.request.call_args[1]
        assert call_kwargs["headers"]["X-App-Token"] == "my-secret-token"

    @patch("handler.http")
    def test_empty_response_returns_empty_list(self, mock_http):
        """Empty API response should return empty list (not error)."""
        from handler import fetch_nssp_data

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps([]).encode()
        mock_http.request.return_value = mock_response

        result = fetch_nssp_data(
            api_base="https://data.cdc.gov/resource",
            dataset_id="rdmq-nq56",
            geography="Texas",
            pathogen="RSV",
            visit_type="ed",
            date_after="2026-06-01",
        )

        assert result == []


class TestStoreToS3:
    """Test S3 storage."""

    @patch("handler.s3")
    def test_store_calls_put_object(self, mock_s3):
        """store_to_s3 should call put_object with correct params."""
        from handler import store_to_s3

        data = {"fetched": [{"pathogen": "Influenza"}]}
        store_to_s3(data, "raw/cdc_nssp/2026/W26/respiratory_activity.json", "my-bucket")

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert "cdc_nssp" in call_kwargs["Key"]
        assert call_kwargs["ContentType"] == "application/json"
