"""Unit tests for alert dispatcher Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock


mock_system_config = {
    "dynamodb_tables": {"subscriptions": "healthsignals-subscriptions-test"},
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


@pytest.fixture(scope="module")
def dispatcher(handler):
    """Expose individual symbols from the handler module."""
    return handler


def _make_event(county_fips="48143", severity="HIGH", disease="influenza"):
    return {
        "disease": disease,
        "severity": severity,
        "county_fips": county_fips,
        "county_name": "Erath County",
        "alert_content": {
            "situation_brief": "Influenza activity elevated in Houston.",
            "email_body": "Detailed email body here.",
            "sms_text": "HealthSignals: HIGH flu alert for Erath County.",
        },
    }


class TestSeverityThreshold:
    """Test severity comparison logic."""

    def test_exact_threshold_met(self, handler):
        assert handler._meets_threshold("HIGH", "HIGH") is True

    def test_above_threshold_met(self, handler):
        assert handler._meets_threshold("CRITICAL", "HIGH") is True
        assert handler._meets_threshold("HIGH", "MODERATE") is True

    def test_below_threshold_not_met(self, handler):
        assert handler._meets_threshold("LOW", "MODERATE") is False
        assert handler._meets_threshold("MODERATE", "HIGH") is False

    def test_critical_always_sent(self, handler):
        """CRITICAL alert should pass every threshold."""
        for threshold in handler.SEVERITY_ORDER:
            assert handler._meets_threshold("CRITICAL", threshold) is True

    def test_low_only_sent_for_low_threshold(self, handler):
        """LOW alert should only pass LOW threshold."""
        assert handler._meets_threshold("LOW", "LOW") is True
        assert handler._meets_threshold("LOW", "MODERATE") is False

    def test_case_insensitive(self, handler):
        assert handler._meets_threshold("high", "moderate") is True
        assert handler._meets_threshold("HIGH", "moderate") is True


class TestGetActiveSubscribers:
    """Test subscriber filtering from DynamoDB."""

    def _make_sub(self, **overrides):
        base = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "verified_at": "2026-01-01T00:00:00",
            "diseases": ["influenza", "rsv"],
            "delivery_preferences": {"channels": ["email"], "alert_threshold": "MODERATE"},
            "contact_email": "officer@county.gov",
        }
        return {**base, **overrides}

    def test_active_verified_subscriber_returned(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [self._make_sub()]}
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 1

    def test_unverified_subscriber_excluded(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [self._make_sub(verified_at=None)]}
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 0

    def test_inactive_subscriber_excluded(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": [self._make_sub(status="inactive")]}
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 0

    def test_paused_subscriber_excluded(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {
                "Items": [self._make_sub(status="paused", pause_until="2099-12-31T00:00:00")]
            }
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 0

    def test_expired_pause_subscriber_included(self, handler):
        """Pause that already expired should not block delivery."""
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {
                "Items": [self._make_sub(pause_until="2020-01-01T00:00:00")]
            }
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 1

    def test_unsubscribed_disease_excluded(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {
                "Items": [self._make_sub(diseases=["rsv"])]  # Not subscribed to flu
            }
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 0

    def test_below_alert_threshold_excluded(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {
                "Items": [self._make_sub(
                    delivery_preferences={"channels": ["email"], "alert_threshold": "CRITICAL"}
                )]
            }
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert len(result) == 0

    def test_query_failure_returns_empty(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.side_effect = Exception("DynamoDB error")
            result = handler._get_active_subscribers("48143", "influenza", "HIGH")
        assert result == []


class TestFullHandler:
    """End-to-end handler tests."""

    def _active_sub(self):
        return {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "verified_at": "2026-01-01T00:00:00",
            "diseases": ["influenza"],
            "delivery_preferences": {"channels": ["email"], "alert_threshold": "MODERATE"},
            "contact_email": "officer@county.gov",
        }

    def test_dispatches_to_subscriber_via_email(self, handler):
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "_update_last_alert"):
            mock_table.query.return_value = {"Items": [self._active_sub()]}
            result = handler.lambda_handler(_make_event(), None)

        mock_ses.send_email.assert_called_once()
        assert result["total_dispatched"] == 1
        assert result["success"] is True

    def test_dispatches_sms_when_channel_configured(self, handler):
        sub = {
            **self._active_sub(),
            "delivery_preferences": {"channels": ["email", "sms"], "alert_threshold": "LOW"},
            "contact_phone": "+15551234567",
        }
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "sns") as mock_sns, \
             patch.object(handler, "_update_last_alert"):
            mock_table.query.return_value = {"Items": [sub]}
            handler.lambda_handler(_make_event(), None)

        mock_sns.publish.assert_called_once()

    def test_unsubscribe_url_appended_to_email(self, handler):
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "_update_last_alert"), \
             patch.object(handler, "generate_unsubscribe_url",
                          return_value="https://api.example.com/unsubscribe?token=abc"):
            mock_table.query.return_value = {"Items": [self._active_sub()]}
            handler.lambda_handler(_make_event(), None)

        email_call = mock_ses.send_email.call_args[1]
        body_text = email_call["Message"]["Body"]["Text"]["Data"]
        assert "unsubscribe" in body_text.lower()

    def test_last_alert_sent_updated(self, handler):
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses"), \
             patch.object(handler, "_update_last_alert") as mock_update:
            mock_table.query.return_value = {"Items": [self._active_sub()]}
            handler.lambda_handler(_make_event(), None)

        mock_update.assert_called_once_with("48143", "sub-001")

    def test_no_subscribers_uses_config_fallback(self, handler):
        event = {**_make_event(), "county": {"contacts": {"health_officer": {"email": "officer@county.gov"}}}}
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses:
            mock_table.query.return_value = {"Items": []}
            result = handler.lambda_handler(event, None)

        assert result["total_dispatched"] == 1

    def test_no_subscribers_no_config_skipped(self, handler):
        with patch.object(handler, "sub_table") as mock_table:
            mock_table.query.return_value = {"Items": []}
            result = handler.lambda_handler(_make_event(), None)

        assert result["total_dispatched"] == 0
        assert len(result["details"]["skipped"]) == 1

    def test_missing_county_fips_returns_error(self, handler):
        event = {"disease": "influenza", "severity": "HIGH"}
        result = handler.lambda_handler(event, None)
        assert result.get("dispatched") is False

    def test_email_send_failure_captured(self, handler):
        with patch.object(handler, "sub_table") as mock_table, \
             patch.object(handler, "ses") as mock_ses, \
             patch.object(handler, "_update_last_alert"):
            mock_table.query.return_value = {"Items": [self._active_sub()]}
            mock_ses.send_email.side_effect = Exception("SES throttled")
            result = handler.lambda_handler(_make_event(), None)

        assert result["total_errors"] == 1
        assert result["total_dispatched"] == 0
