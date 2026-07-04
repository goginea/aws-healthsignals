"""Unit tests for Subscription status Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {
    "dynamodb_tables": {"subscriptions": "healthsignals-subscriptions-test", "alert_state": "healthsignals-alert-state-test"},
}


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "subscription/status",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "boto3.resource": MagicMock(),
        },
    )


class TestSubscriptionStatus:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_missing_params_returns_400(self, handler):
        """No county_fips or subscription_id should return 400."""
        event = {"queryStringParameters": {}}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_with_subscription_id(self, handler):
        """Valid subscription_id query should call single subscription lookup."""
        mock_sub = {
            "county_fips": "48143",
            "subscription_id": "abc-123",
            "status": "active",
            "contact_email": "test@test.com",
            "verified_at": "2026-01-15T11:00:00",
        }
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "_get_recent_alerts", return_value=[]):
            mock_table.get_item.return_value = {"Item": mock_sub}
            event = {"queryStringParameters": {"county_fips": "48143", "subscription_id": "abc-123"}}
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200

    def test_not_found_returns_404(self, handler):
        """Non-existent subscription should return 404."""
        with patch.object(handler, "_get_single_subscription", return_value=handler._response(404, {"error": "Subscription not found"})):
            event = {"queryStringParameters": {"county_fips": "48143", "subscription_id": "nonexistent"}}
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 404
