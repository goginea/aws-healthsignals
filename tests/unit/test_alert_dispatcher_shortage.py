"""Unit tests for shortage alert delivery in alert dispatcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock, call


mock_system_config = {
    "dynamodb_tables": {
        "subscriptions": "healthsignals-subscriptions-test",
        "shortage_alerts": "healthsignals-shortage-alerts-test",
    },
    "delivery": {
        "ses_sender_email": "alerts@healthsignals.example.com",
        "max_sms_length": 160,
    },
}


@pytest.fixture(scope="module")
def handler():
    from tests.conftest import load_handler
    return load_handler(
        "delivery/alert_dispatcher",
        extra_patches={
            "shared.config_loader.get_system_config": mock_system_config,
            "boto3.resource": MagicMock(),
            "boto3.client": MagicMock(),
        },
    )


def _make_shortage_event(alert_type="shortage", therapeutic_category="Antivirals"):
    return {
        "alert_type": alert_type,
        "therapeutic_category": therapeutic_category,
        "product_id": "FDA-12345",
        "week_timestamp": "2024-W03",
        "alert_content": {
            "situation_brief": "Antiviral shortage detected.",
            "email_body": "Detailed shortage alert body.",
            "sms_text": "HealthSignals: Antiviral shortage alert.",
        },
    }


def _make_shortage_subscriber(**overrides):
    base = {
        "county_fips": "48143",
        "subscription_id": "sub-shortage-001",
        "status": "active",
        "verified_at": "2026-01-01T00:00:00",
        "therapeutic_categories": ["Antivirals", "Antibiotics"],
        "delivery_preferences": {"channels": ["email"]},
        "contact_email": "pharmacist@hospital.org",
    }
    return {**base, **overrides}


class TestAlertTypeRouting:
    """Test that lambda_handler routes based on alert_type."""

    def test_shortage_alert_routes_to_shortage_dispatch(self, handler):
        event = _make_shortage_event(alert_type="shortage")
        with patch.object(handler, "_dispatch_shortage_alert", return_value={"success": True}) as mock_dispatch:
            handler.lambda_handler(event, None)
        mock_dispatch.assert_called_once_with(event, "shortage")

    def test_combined_alert_routes_to_shortage_dispatch(self, handler):
        event = _make_shortage_event(alert_type="combined")
        with patch.object(handler, "_dispatch_shortage_alert", return_value={"success": True}) as mock_dispatch:
            handler.lambda_handler(event, None)
        mock_dispatch.assert_called_once_with(event, "combined")

    def test_disease_outbreak_routes_to_existing_logic(self, handler):
        event = {
            "alert_type": "disease_outbreak",
            "disease": "influenza",
            "severity": "HIGH",
            "county_fips": "48143",
            "county_name": "Erath County",
            "alert_content": {"email_body": "test"},
        }
        with patch.object(handler, "_dispatch_disease_outbreak_alert", return_value={"success": True}) as mock_dispatch:
            handler.lambda_handler(event, None)
        mock_dispatch.assert_called_once_with(event)

    def test_default_alert_type_is_disease_outbreak(self, handler):
        """When alert_type is missing, default to disease_outbreak path."""
        event = {
            "disease": "influenza",
            "severity": "HIGH",
            "county_fips": "48143",
            "county_name": "Erath County",
            "alert_content": {"email_body": "test"},
        }
        with patch.object(handler, "_dispatch_disease_outbreak_alert", return_value={"success": True}) as mock_dispatch:
            handler.lambda_handler(event, None)
        mock_dispatch.assert_called_once()


class TestShortageAlertDispatch:
    """Test shortage alert delivery logic."""

    def test_missing_therapeutic_category_returns_error(self, handler):
        event = {"alert_type": "shortage", "alert_content": {}}
        result = handler._dispatch_shortage_alert(event, "shortage")
        assert result["dispatched"] is False
        assert "Missing therapeutic_category" in result["error"]

    def test_dispatches_email_to_shortage_subscriber(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "shortage_alerts_table") as mock_alerts_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mock_ses.send_email.assert_called_once()
        assert result["total_dispatched"] == 1
        assert result["success"] is True
        assert result["alert_type"] == "shortage"
        assert result["therapeutic_category"] == "Antivirals"

    def test_email_subject_contains_therapeutic_category(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        email_call = mock_ses.send_email.call_args[1]
        subject = email_call["Message"]["Subject"]["Data"]
        assert "[HealthSignals] Drug Shortage Alert: Antivirals" == subject

    def test_email_body_contains_pharmacist_disclaimer(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        email_call = mock_ses.send_email.call_args[1]
        body = email_call["Message"]["Body"]["Text"]["Data"]
        assert "FOR PHARMACIST REVIEW ONLY" in body

    def test_email_body_contains_unsubscribe_link(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "shortage_alerts_table"), \
             patch.object(handler, "generate_unsubscribe_url",
                          return_value="https://api.example.com/subscription/unsubscribe?token=xyz"):
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        email_call = mock_ses.send_email.call_args[1]
        body = email_call["Message"]["Body"]["Text"]["Data"]
        assert "unsubscribe" in body.lower()

    def test_sms_sent_when_channel_enabled_and_phone_present(self, handler):
        sub = _make_shortage_subscriber(
            delivery_preferences={"channels": ["email", "sms"]},
            contact_phone="+15551234567",
        )
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "sns") as mock_sns, \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mock_sns.publish.assert_called_once()
        sms_message = mock_sns.publish.call_args[1]["Message"]
        assert len(sms_message) <= 160

    def test_sms_not_sent_when_channel_disabled(self, handler):
        sub = _make_shortage_subscriber(
            delivery_preferences={"channels": ["email"]},
            contact_phone="+15551234567",
        )
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "sns") as mock_sns, \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mock_sns.publish.assert_not_called()

    def test_no_subscribers_skipped(self, handler):
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": []}
            result = handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        assert result["total_dispatched"] == 0
        assert result["success"] is False
        assert len(result["details"]["skipped"]) == 1

    def test_shortage_alerts_table_updated_on_success(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "shortage_alerts_table") as mock_alerts_table:
            mock_table.query.return_value = {"Items": [sub]}
            handler._dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mock_alerts_table.update_item.assert_called_once()
        call_kwargs = mock_alerts_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"product_id": "FDA-12345", "week_timestamp": "2024-W03"}
        assert call_kwargs["ExpressionAttributeValues"][":status"] == "SENT"
        assert call_kwargs["ExpressionAttributeValues"][":count"] == 1


class TestGetShortageSubscribers:
    """Test GSI query and filtering for shortage subscribers."""

    def test_active_verified_subscriber_returned(self, handler):
        sub = _make_shortage_subscriber()
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._get_shortage_subscribers("Antivirals")
        assert len(result) == 1

    def test_queries_correct_gsi(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            handler._get_shortage_subscribers("Antivirals")

        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "therapeutic-category-lookup"

    def test_inactive_subscriber_excluded(self, handler):
        sub = _make_shortage_subscriber(status="inactive")
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_unverified_subscriber_excluded(self, handler):
        sub = _make_shortage_subscriber(verified_at=None)
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_paused_subscriber_excluded(self, handler):
        sub = _make_shortage_subscriber(pause_until="2099-12-31T00:00:00")
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_expired_pause_subscriber_included(self, handler):
        sub = _make_shortage_subscriber(pause_until="2020-01-01T00:00:00")
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._get_shortage_subscribers("Antivirals")
        assert len(result) == 1

    def test_query_failure_returns_empty(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.side_effect = Exception("DynamoDB error")
            result = handler._get_shortage_subscribers("Antivirals")
        assert result == []


class TestCombinedAlertType:
    """Test combined alert type works the same as shortage for delivery."""

    def test_combined_alert_dispatches_successfully(self, handler):
        sub = _make_shortage_subscriber()
        event = _make_shortage_event(alert_type="combined")
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "shortage_alerts_table"):
            mock_table.query.return_value = {"Items": [sub]}
            result = handler._dispatch_shortage_alert(event, "combined")

        assert result["alert_type"] == "combined"
        assert result["success"] is True
