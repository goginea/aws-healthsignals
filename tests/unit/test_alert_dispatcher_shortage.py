"""Unit tests for shortage alert dispatch plugin (shortage_dispatch.py).

Tests the plugin module that handles 'shortage' and 'combined' alert types
via the alert dispatcher registry pattern.
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call

# Ensure lambdas directory is on path for imports
LAMBDAS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
sys.path.insert(0, os.path.join(LAMBDAS_ROOT, "delivery", "alert_dispatcher"))
sys.path.insert(0, os.path.join(LAMBDAS_ROOT, "shared"))
sys.path.insert(0, LAMBDAS_ROOT)

from tests.conftest import load_handler


mock_system_config = {
    "dynamodb_tables": {
        "subscriptions": "healthsignals-subscriptions-test",
    },
    "delivery": {
        "ses_sender_email": "alerts@healthsignals.example.com",
        "max_sms_length": 160,
    },
}


@pytest.fixture(scope="module")
def shortage_module():
    """Load and register the shortage_dispatch plugin module."""
    os.environ["SHORTAGE_ALERTS_TABLE"] = "healthsignals-shortage-alerts-test"

    # Import the module directly
    import importlib.util
    module_path = os.path.join(LAMBDAS_ROOT, "delivery", "alert_dispatcher", "shortage_dispatch.py")
    spec = importlib.util.spec_from_file_location("shortage_dispatch", module_path)
    import types
    module = types.ModuleType("shortage_dispatch")
    module.__spec__ = spec
    module.__file__ = module_path

    # Patch boto3 before loading
    with patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)

    # Register with mock context
    mock_sub_table = MagicMock()
    mock_ses = MagicMock()
    mock_sns = MagicMock()
    mock_dynamodb = MagicMock()
    mock_shortage_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_shortage_table

    context = {
        "sub_table": mock_sub_table,
        "ses": mock_ses,
        "sns": mock_sns,
        "system": mock_system_config,
        "api_base_url": "https://api.test.com",
        "dynamodb": mock_dynamodb,
    }
    module.register(context)

    # Attach mocks for test access
    module._test_mocks = {
        "sub_table": mock_sub_table,
        "ses": mock_ses,
        "sns": mock_sns,
        "shortage_alerts_table": mock_shortage_table,
    }

    return module


@pytest.fixture(scope="module")
def handler():
    """Load the main alert_dispatcher handler for routing tests."""
    os.environ["DISPATCH_PLUGINS"] = "shortage_dispatch"
    os.environ["SHORTAGE_ALERTS_TABLE"] = "healthsignals-shortage-alerts-test"

    return load_handler(
        "delivery/alert_dispatcher",
        extra_patches={
            "shared.config_loader.get_system_config": mock_system_config,
            "shared.token_utils.generate_unsubscribe_url": "https://unsub.test/token",
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
    """Test that lambda_handler routes based on alert_type via registry."""

    def test_disease_outbreak_routes_to_core_handler(self, handler):
        """disease_outbreak is handled by core handler."""
        assert "disease_outbreak" in handler._dispatch_registry

    def test_unknown_alert_type_returns_error(self, handler):
        event = {"alert_type": "unknown_module", "alert_content": {}}
        result = handler.lambda_handler(event, None)
        assert result["dispatched"] is False
        assert "Unknown alert_type" in result["error"]

    def test_registry_accepts_plugin_registration(self, handler):
        """Plugins can register new alert_type handlers at runtime."""
        mock_handler = MagicMock(return_value={"success": True})
        handler._dispatch_registry["test_plugin"] = mock_handler

        event = {"alert_type": "test_plugin", "data": "test"}
        result = handler.lambda_handler(event, None)

        mock_handler.assert_called_once_with(event, "test_plugin")
        assert result == {"success": True}

        # Cleanup
        del handler._dispatch_registry["test_plugin"]

    def test_default_alert_type_is_disease_outbreak(self, handler):
        """When alert_type is missing, default to disease_outbreak."""
        event = {
            "disease": "influenza",
            "severity": "HIGH",
            "county_fips": "48143",
            "county_name": "Erath County",
            "alert_content": {"email_body": "test"},
        }
        # Should not error — routes to core disease_outbreak handler
        result = handler.lambda_handler(event, None)
        # The handler runs (even if no real subscribers), it doesn't return an error
        assert "error" not in result or "Unknown alert_type" not in result.get("error", "")


class TestShortageAlertDispatch:
    """Test shortage alert delivery logic via the plugin module."""

    def test_missing_therapeutic_category_returns_error(self, shortage_module):
        event = {"alert_type": "shortage", "alert_content": {}}
        result = shortage_module.dispatch_shortage_alert(event, "shortage")
        assert result["dispatched"] is False
        assert "Missing therapeutic_category" in result["error"]

    def test_dispatches_email_to_shortage_subscriber(self, shortage_module):
        sub = _make_shortage_subscriber()
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}
        mocks["ses"].reset_mock()

        with patch("shared.token_utils.generate_unsubscribe_url", return_value="https://unsub.test"):
            result = shortage_module.dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mocks["ses"].send_email.assert_called()
        assert result["total_dispatched"] == 1
        assert result["success"] is True
        assert result["alert_type"] == "shortage"

    def test_no_subscribers_returns_skipped(self, shortage_module):
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": []}

        result = shortage_module.dispatch_shortage_alert(_make_shortage_event(), "shortage")

        assert result["total_dispatched"] == 0
        assert result["success"] is False

    def test_shortage_alerts_table_updated_on_success(self, shortage_module):
        sub = _make_shortage_subscriber()
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}
        mocks["shortage_alerts_table"].reset_mock()

        with patch("shared.token_utils.generate_unsubscribe_url", return_value="https://unsub.test"):
            shortage_module.dispatch_shortage_alert(_make_shortage_event(), "shortage")

        mocks["shortage_alerts_table"].update_item.assert_called_once()


class TestGetShortageSubscribers:
    """Test subscriber filtering for shortage alerts."""

    def test_active_verified_subscriber_returned(self, shortage_module):
        sub = _make_shortage_subscriber()
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert len(result) == 1

    def test_queries_correct_gsi(self, shortage_module):
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": []}

        shortage_module._get_shortage_subscribers("Antibiotics")

        call_kwargs = mocks["sub_table"].query.call_args[1]
        assert call_kwargs["IndexName"] == "alert-category-lookup"

    def test_inactive_subscriber_excluded(self, shortage_module):
        sub = _make_shortage_subscriber(status="cancelled")
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_unverified_subscriber_excluded(self, shortage_module):
        sub = _make_shortage_subscriber(verified_at=None)
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_paused_subscriber_excluded(self, shortage_module):
        sub = _make_shortage_subscriber(pause_until="2099-01-01T00:00:00")
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert len(result) == 0

    def test_expired_pause_subscriber_included(self, shortage_module):
        sub = _make_shortage_subscriber(pause_until="2020-01-01T00:00:00")
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert len(result) == 1

    def test_query_failure_returns_empty(self, shortage_module):
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.side_effect = Exception("DDB error")

        result = shortage_module._get_shortage_subscribers("Antivirals")
        assert result == []
        mocks["sub_table"].query.side_effect = None


class TestCombinedAlertType:
    """Test combined alert type routing."""

    def test_combined_alert_dispatches_successfully(self, shortage_module):
        sub = _make_shortage_subscriber()
        mocks = shortage_module._test_mocks
        mocks["sub_table"].query.return_value = {"Items": [sub]}
        mocks["ses"].reset_mock()

        event = _make_shortage_event(alert_type="combined")
        with patch("shared.token_utils.generate_unsubscribe_url", return_value="https://unsub.test"):
            result = shortage_module.dispatch_shortage_alert(event, "combined")

        assert result["alert_type"] == "combined"
        assert result["success"] is True
