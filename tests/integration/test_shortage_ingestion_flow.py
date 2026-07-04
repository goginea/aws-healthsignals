"""Integration tests — openFDA Shortage Fetcher Lambda end-to-end flow.

Tests the fetcher Lambda handler with mocked HTTP responses to verify:
1. S3 storage with correct key patterns
2. Pagination across multiple API pages
3. Retry logic for transient 503 errors
4. DLQ behavior after max retry exhaustion
5. 404 handling for changed endpoints

Run with: pytest tests/integration/test_shortage_ingestion_flow.py -v
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime

# Ensure paths are set up for test imports
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "shared"))
sys.path.insert(0, os.path.join(ROOT, "lambdas"))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher"))


# Load test fixtures
FIXTURES_PATH = os.path.join(ROOT, "tests", "data", "openfda_mock_responses.json")
with open(FIXTURES_PATH) as f:
    FIXTURES = json.load(f)


def _make_http_response(status, data):
    """Create a mock urllib3 response object."""
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.data = json.dumps(data).encode("utf-8")
    return mock_response


def _build_sqs_event():
    """Build a minimal SQS trigger event like EventBridge would produce."""
    return {
        "Records": [
            {
                "body": json.dumps({"source": "scheduled", "trigger": "weekly_monday"})
            }
        ]
    }


def _mock_config():
    """Return the data source config for openfda_shortages."""
    return {
        "source_name": "openfda_drug_shortages",
        "enabled": True,
        "api": {
            "base_url": "https://api.fda.gov/drug/shortages.json",
            "auth_type": "none",
            "rate_limit_per_hour": 240,
            "timeout_seconds": 30,
            "retry_max_attempts": 3,
            "retry_backoff_seconds": 0,
            "pagination": {
                "enabled": True,
                "limit_param": "limit",
                "default_limit": 1000,
            },
        },
        "s3_storage": {
            "prefix_pattern": "raw/openfda-shortages/{year}/W{week}/shortages_{timestamp}.json"
        },
    }


def _mock_system_config():
    """Return mock system config."""
    return {
        "infrastructure": {
            "data_bucket_name_pattern": "healthsignals-data-test"
        }
    }


def _mock_therapeutic_config():
    """Return a minimal therapeutic category config for tests."""
    return {
        "categories": [
            {
                "category_key": "antivirals",
                "display_name": "Antivirals",
                "priority_level": "HIGH",
                "relevant_diseases": ["influenza", "covid"],
                "fda_classification_mapping": ["*oseltamivir*", "*tamiflu*"],
            },
            {
                "category_key": "antibiotics",
                "display_name": "Antibiotics",
                "priority_level": "HIGH",
                "relevant_diseases": ["influenza", "covid", "rsv"],
                "fda_classification_mapping": ["*amoxicillin*", "*azithromycin*"],
            },
        ]
    }


class TestFetcherStoresToS3:
    """Test that the fetcher Lambda stores normalized records to S3 correctly."""

    @patch("time.sleep", return_value=None)
    def test_fetcher_stores_to_s3(self, mock_sleep):
        """Invoke handler with test event, verify S3 put_object called with correct key pattern."""
        fixture = FIXTURES["scenario_new_shortages"]

        mock_s3 = MagicMock()
        mock_cloudwatch = MagicMock()
        mock_http = MagicMock()

        # Mock single page API response
        mock_http.request.return_value = _make_http_response(200, fixture)

        with patch("shared.config_loader.get_data_source_config", return_value=_mock_config()), \
             patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
             patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()):

            # Import the handler after patching config_loader
            if "handler" in sys.modules:
                del sys.modules["handler"]
            handler_dir = os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher")
            old_path = sys.path.copy()
            sys.path.insert(0, handler_dir)

            try:
                with patch("boto3.client") as mock_boto_client:
                    # Route boto3.client calls to appropriate mocks
                    def client_factory(service, **kwargs):
                        if service == "s3":
                            return mock_s3
                        elif service == "cloudwatch":
                            return mock_cloudwatch
                        return MagicMock()

                    mock_boto_client.side_effect = client_factory

                    with patch("urllib3.PoolManager") as mock_pool:
                        mock_pool.return_value = mock_http

                        # Force reimport
                        for mod_name in list(sys.modules.keys()):
                            if "openfda" in mod_name or mod_name == "handler":
                                del sys.modules[mod_name]

                        import importlib
                        import handler as fetcher_module
                        importlib.reload(fetcher_module)

                        # Override module-level clients
                        fetcher_module.s3 = mock_s3
                        fetcher_module.cloudwatch = mock_cloudwatch
                        fetcher_module.http = mock_http

                        result = fetcher_module.lambda_handler(_build_sqs_event(), None)

                        # Verify S3 put_object was called
                        assert mock_s3.put_object.called, "S3 put_object should be called"
                        call_kwargs = mock_s3.put_object.call_args[1]

                        # Verify key pattern
                        s3_key = call_kwargs["Key"]
                        assert "raw/openfda-shortages/" in s3_key
                        assert s3_key.endswith(".json")

                        # Verify bucket
                        assert call_kwargs["Bucket"] == "healthsignals-data-test"

                        # Verify content type
                        assert call_kwargs["ContentType"] == "application/json"

                        # Verify response
                        assert result["statusCode"] == 200
                        assert "s3_key" in result
            finally:
                sys.path = old_path


class TestFetcherPagination:
    """Test fetcher handles pagination across multiple API pages."""

    @patch("time.sleep", return_value=None)
    def test_fetcher_pagination(self, mock_sleep):
        """Mock 2 pages of results (1000 + 500), verify 1500 records stored."""
        # Page 1: 1000 results
        page1_results = [
            {
                "product_id": f"PROD-{i:04d}",
                "productName": f"Product {i}",
                "currentSupplyStatus": "Available",
                "reason": "Test",
            }
            for i in range(1000)
        ]
        page1 = {"results": page1_results}

        # Page 2: 500 results (less than limit, so pagination ends)
        page2_results = [
            {
                "product_id": f"PROD-{i:04d}",
                "productName": f"Product {i}",
                "currentSupplyStatus": "Available",
                "reason": "Test",
            }
            for i in range(1000, 1500)
        ]
        page2 = {"results": page2_results}

        mock_s3 = MagicMock()
        mock_cloudwatch = MagicMock()
        mock_http = MagicMock()

        # Return page1 on first call, page2 on second
        mock_http.request.side_effect = [
            _make_http_response(200, page1),
            _make_http_response(200, page2),
        ]

        with patch("shared.config_loader.get_data_source_config", return_value=_mock_config()), \
             patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
             patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()):

            handler_dir = os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher")
            old_path = sys.path.copy()
            sys.path.insert(0, handler_dir)

            try:
                for mod_name in list(sys.modules.keys()):
                    if "openfda" in mod_name or mod_name == "handler":
                        del sys.modules[mod_name]

                import importlib
                import handler as fetcher_module
                importlib.reload(fetcher_module)

                fetcher_module.s3 = mock_s3
                fetcher_module.cloudwatch = mock_cloudwatch
                fetcher_module.http = mock_http

                result = fetcher_module.lambda_handler(_build_sqs_event(), None)

                # Verify 2 HTTP requests were made (pagination)
                assert mock_http.request.call_count == 2

                # Verify all 1500 records were stored
                assert result["statusCode"] == 200
                assert result["records_fetched"] == 1500

                # Verify S3 body contains 1500 records
                call_kwargs = mock_s3.put_object.call_args[1]
                stored_data = json.loads(call_kwargs["Body"])
                assert len(stored_data) == 1500
            finally:
                sys.path = old_path


class TestFetcherRetryOn503:
    """Test fetcher retries on HTTP 503 errors."""

    @patch("time.sleep", return_value=None)
    def test_fetcher_retry_on_503(self, mock_sleep):
        """Mock first request returns 503, second succeeds, verify retry worked."""
        fixture = FIXTURES["scenario_new_shortages"]

        mock_s3 = MagicMock()
        mock_cloudwatch = MagicMock()
        mock_http = MagicMock()

        # First call returns 503, second returns 200
        mock_http.request.side_effect = [
            _make_http_response(503, {"error": "Service Unavailable"}),
            _make_http_response(200, fixture),
        ]

        with patch("shared.config_loader.get_data_source_config", return_value=_mock_config()), \
             patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
             patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()):

            handler_dir = os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher")
            old_path = sys.path.copy()
            sys.path.insert(0, handler_dir)

            try:
                for mod_name in list(sys.modules.keys()):
                    if "openfda" in mod_name or mod_name == "handler":
                        del sys.modules[mod_name]

                import importlib
                import handler as fetcher_module
                importlib.reload(fetcher_module)

                fetcher_module.s3 = mock_s3
                fetcher_module.cloudwatch = mock_cloudwatch
                fetcher_module.http = mock_http

                result = fetcher_module.lambda_handler(_build_sqs_event(), None)

                # Should have retried and succeeded
                assert result["statusCode"] == 200
                # HTTP called at least 2 times (1 failure + 1 success)
                assert mock_http.request.call_count >= 2
                # S3 should have received data
                assert mock_s3.put_object.called
            finally:
                sys.path = old_path


class TestFetcherDLQAfterMaxRetries:
    """Test fetcher raises exception after max retries (SQS handles DLQ)."""

    @patch("time.sleep", return_value=None)
    def test_fetcher_sends_to_dlq_after_max_retries(self, mock_sleep):
        """Mock all requests return 500, verify exception raised (SQS handles DLQ)."""
        mock_s3 = MagicMock()
        mock_cloudwatch = MagicMock()
        mock_http = MagicMock()

        # All requests return 500
        mock_http.request.return_value = _make_http_response(
            500, {"error": "Internal Server Error"}
        )

        with patch("shared.config_loader.get_data_source_config", return_value=_mock_config()), \
             patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
             patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()):

            handler_dir = os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher")
            old_path = sys.path.copy()
            sys.path.insert(0, handler_dir)

            try:
                for mod_name in list(sys.modules.keys()):
                    if "openfda" in mod_name or mod_name == "handler":
                        del sys.modules[mod_name]

                import importlib
                import handler as fetcher_module
                importlib.reload(fetcher_module)

                fetcher_module.s3 = mock_s3
                fetcher_module.cloudwatch = mock_cloudwatch
                fetcher_module.http = mock_http

                # Should raise RuntimeError after exhausting retries
                with pytest.raises(RuntimeError, match="500"):
                    fetcher_module.lambda_handler(_build_sqs_event(), None)

                # Verify max retries attempted (initial + 3 retries = 4 calls)
                assert mock_http.request.call_count == 4
            finally:
                sys.path = old_path


class TestFetcherSkips404:
    """Test fetcher raises RuntimeError on 404 without retrying."""

    @patch("time.sleep", return_value=None)
    def test_fetcher_skips_404(self, mock_sleep):
        """Mock 404 response, verify RuntimeError raised with 'endpoint may have changed'."""
        mock_s3 = MagicMock()
        mock_cloudwatch = MagicMock()
        mock_http = MagicMock()

        # Return 404
        mock_http.request.return_value = _make_http_response(
            404, {"error": "Not Found"}
        )

        with patch("shared.config_loader.get_data_source_config", return_value=_mock_config()), \
             patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
             patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()):

            handler_dir = os.path.join(ROOT, "lambdas", "ingestion", "openfda_shortage_fetcher")
            old_path = sys.path.copy()
            sys.path.insert(0, handler_dir)

            try:
                for mod_name in list(sys.modules.keys()):
                    if "openfda" in mod_name or mod_name == "handler":
                        del sys.modules[mod_name]

                import importlib
                import handler as fetcher_module
                importlib.reload(fetcher_module)

                fetcher_module.s3 = mock_s3
                fetcher_module.cloudwatch = mock_cloudwatch
                fetcher_module.http = mock_http

                # Should raise RuntimeError mentioning endpoint change
                with pytest.raises(RuntimeError, match="(?i)endpoint may have changed"):
                    fetcher_module.lambda_handler(_build_sqs_event(), None)

                # Should NOT retry on 404 — only 1 request made
                assert mock_http.request.call_count == 1
            finally:
                sys.path = old_path
