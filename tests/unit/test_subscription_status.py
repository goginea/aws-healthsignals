"""Unit tests for Subscription Status Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas/subscription/status"))


MOCK_SUBSCRIPTION = {
    "county_fips": "48143",
    "subscription_id": "sub-uuid-001",
    "contact_name": "Dr. Smith",
    "contact_email": "smith@erath.gov",
    "contact_phone": "+15551234567",
    "diseases": ["influenza", "rsv"],
    "delivery_preferences": {"channels": ["email", "sms"], "alert_threshold": "MODERATE"},
    "status": "active",
    "verified_at": "2026-06-01T10:00:00",
    "created_at": "2026-05-30T08:00:00",
    "updated_at": "2026-06-15T12:00:00",
    "last_alert_sent": "2026-01-15T09:00:00",
    "verification_token": "secret-token-xyz",  # Should be stripped
    "unsubscribe_token": "unsub-token-abc",  # Should be stripped
}


@patch("handler.get_system_config", return_value={
    "dynamodb_tables": {"subscriptions": "test-subs", "alert_state": "test-alerts"}
})
class TestSubscriptionStatus:
    """Test subscription status endpoint."""

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_get_single_subscription_success(self, mock_sub_table, mock_alert_table, mock_sys):
        """Valid county_fips + subscription_id returns subscription details."""
        from handler import lambda_handler

        mock_sub_table.get_item.return_value = {"Item": MOCK_SUBSCRIPTION.copy()}
        mock_alert_table.query.return_value = {"Items": []}

        event = {
            "queryStringParameters": {
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
            }
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["county_fips"] == "48143"
        assert body["contact_email"] == "smith@erath.gov"
        # Sensitive fields should be stripped
        assert "verification_token" not in body
        assert "unsubscribe_token" not in body

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_subscription_not_found_returns_404(self, mock_sub_table, mock_alert_table, mock_sys):
        """Non-existent subscription returns 404."""
        mock_sub_table.get_item.return_value = {}  # No Item key

        event = {
            "queryStringParameters": {
                "county_fips": "48999",
                "subscription_id": "nonexistent",
            }
        }

        from handler import lambda_handler
        result = lambda_handler(event, None)

        assert result["statusCode"] == 404

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_missing_county_fips_returns_400(self, mock_sub_table, mock_alert_table, mock_sys):
        """Missing county_fips parameter returns 400."""
        from handler import lambda_handler

        event = {"queryStringParameters": {}}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
        assert "county_fips" in json.loads(result["body"])["error"]

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_health_assessment_healthy(self, mock_sub_table, mock_alert_table, mock_sys):
        """Active, verified subscription with phone → healthy."""
        from handler import lambda_handler

        mock_sub_table.get_item.return_value = {"Item": MOCK_SUBSCRIPTION.copy()}
        mock_alert_table.query.return_value = {"Items": []}

        event = {
            "queryStringParameters": {
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
            }
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert body["health"]["status"] == "healthy"
        assert body["health"]["issues"] == []

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_health_assessment_missing_phone_for_sms(self, mock_sub_table, mock_alert_table, mock_sys):
        """SMS configured but no phone → health issue flagged."""
        from handler import lambda_handler

        sub = MOCK_SUBSCRIPTION.copy()
        sub["contact_phone"] = ""  # No phone
        mock_sub_table.get_item.return_value = {"Item": sub}
        mock_alert_table.query.return_value = {"Items": []}

        event = {
            "queryStringParameters": {
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
            }
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert body["health"]["status"] == "attention_needed"
        assert any("SMS" in issue and "phone" in issue for issue in body["health"]["issues"])

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_health_assessment_pending_verification(self, mock_sub_table, mock_alert_table, mock_sys):
        """Pending verification → health issue flagged."""
        from handler import lambda_handler

        sub = MOCK_SUBSCRIPTION.copy()
        sub["status"] = "pending_verification"
        mock_sub_table.get_item.return_value = {"Item": sub}
        mock_alert_table.query.return_value = {"Items": []}

        event = {
            "queryStringParameters": {
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
            }
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert body["health"]["status"] == "attention_needed"
        assert any("verified" in issue.lower() for issue in body["health"]["issues"])

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_get_all_county_subscriptions(self, mock_sub_table, mock_alert_table, mock_sys):
        """Without subscription_id, return all subscriptions for county."""
        from handler import lambda_handler

        mock_sub_table.query.return_value = {
            "Items": [MOCK_SUBSCRIPTION.copy(), {**MOCK_SUBSCRIPTION, "subscription_id": "sub-002"}]
        }

        event = {"queryStringParameters": {"county_fips": "48143"}}

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["subscription_count"] == 2

    @patch("handler.alert_table")
    @patch("handler.sub_table")
    def test_null_query_params_handled(self, mock_sub_table, mock_alert_table, mock_sys):
        """None queryStringParameters should return 400."""
        from handler import lambda_handler

        event = {"queryStringParameters": None}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
