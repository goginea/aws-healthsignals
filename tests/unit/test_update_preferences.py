"""Unit tests for Update Preferences Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas/subscription/update_preferences"))


MOCK_ACTIVE_SUBSCRIPTION = {
    "county_fips": "48143",
    "subscription_id": "sub-uuid-001",
    "contact_email": "old@erath.gov",
    "contact_phone": "+15551234567",
    "diseases": ["influenza", "rsv", "covid"],
    "delivery_preferences": {"channels": ["email"], "alert_threshold": "MODERATE"},
    "status": "active",
}


@patch("handler.get_system_config", return_value={
    "dynamodb_tables": {"subscriptions": "test-subs"}
})
@patch("handler.list_active_diseases", return_value=["influenza", "rsv", "covid"])
class TestUpdatePreferences:
    """Test subscription preference updates."""

    @patch("handler.table")
    def test_update_email_success(self, mock_table, mock_diseases, mock_sys):
        """Valid email update should succeed."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}
        mock_table.update_item.return_value = {
            "Attributes": {**MOCK_ACTIVE_SUBSCRIPTION, "contact_email": "new@erath.gov", "status": "active"}
        }

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"contact_email": "new@erath.gov"},
            })
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert "contact_email" in body["updated_fields"]

    @patch("handler.table")
    def test_pause_subscription(self, mock_table, mock_diseases, mock_sys):
        """Setting pause_until should change status to paused."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}
        mock_table.update_item.return_value = {
            "Attributes": {**MOCK_ACTIVE_SUBSCRIPTION, "status": "paused", "pause_until": "2026-10-01"}
        }

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"pause_until": "2026-10-01"},
            })
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["current_status"] == "paused"
        assert body["pause_until"] == "2026-10-01"

    @patch("handler.table")
    def test_resume_paused_subscription(self, mock_table, mock_diseases, mock_sys):
        """Setting pause_until to null should resume (status → active)."""
        from handler import lambda_handler

        paused_sub = {**MOCK_ACTIVE_SUBSCRIPTION, "status": "paused", "pause_until": "2026-10-01"}
        mock_table.get_item.return_value = {"Item": paused_sub}
        mock_table.update_item.return_value = {
            "Attributes": {**MOCK_ACTIVE_SUBSCRIPTION, "status": "active", "pause_until": None}
        }

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"pause_until": None},
            })
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["current_status"] == "active"

    @patch("handler.table")
    def test_invalid_email_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Invalid email format should return 400 validation error."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"contact_email": "not-an-email"},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
        assert "validation" in json.loads(result["body"])["error"]

    @patch("handler.table")
    def test_invalid_phone_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Invalid phone (not E.164) should return 400."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"contact_phone": "555-123-4567"},  # Not E.164
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400

    @patch("handler.table")
    def test_invalid_disease_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Non-existent disease in updates should return 400."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"diseases": ["influenza", "bubonic_plague"]},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
        assert "bubonic_plague" in json.loads(result["body"])["message"]

    @patch("handler.table")
    def test_missing_required_fields_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Missing county_fips or subscription_id should return 400."""
        from handler import lambda_handler

        event = {
            "body": json.dumps({
                "updates": {"contact_email": "test@test.com"},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400

    @patch("handler.table")
    def test_no_updates_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Empty updates dict should return 400."""
        from handler import lambda_handler

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400

    @patch("handler.table")
    def test_inactive_subscription_returns_410(self, mock_table, mock_diseases, mock_sys):
        """Updating an inactive (unsubscribed) subscription should return 410."""
        from handler import lambda_handler

        inactive_sub = {**MOCK_ACTIVE_SUBSCRIPTION, "status": "inactive"}
        mock_table.get_item.return_value = {"Item": inactive_sub}

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"contact_email": "new@test.com"},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 410

    @patch("handler.table")
    def test_invalid_delivery_channel_returns_400(self, mock_table, mock_diseases, mock_sys):
        """Invalid delivery channel should return 400."""
        from handler import lambda_handler

        mock_table.get_item.return_value = {"Item": MOCK_ACTIVE_SUBSCRIPTION.copy()}

        event = {
            "body": json.dumps({
                "county_fips": "48143",
                "subscription_id": "sub-uuid-001",
                "updates": {"delivery_preferences": {"channels": ["email", "telegram"]}},
            })
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
