"""Unit tests for CDC Outbreak Alert Dispatch Plugin.

Tests the outbreak_dispatch module: registration, state-based subscriber lookup,
and delivery routing.
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Ensure paths
LAMBDAS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
sys.path.insert(0, os.path.join(LAMBDAS_ROOT, "delivery", "alert_dispatcher"))
sys.path.insert(0, os.path.join(LAMBDAS_ROOT, "shared"))
sys.path.insert(0, LAMBDAS_ROOT)


MOCK_SYSTEM = {
    "delivery": {
        "ses_sender_email": "alerts@healthsignals.example.com",
        "max_sms_length": 160,
    },
}


@pytest.fixture(scope="module")
def outbreak_module():
    """Load and register the outbreak_dispatch plugin module."""
    import importlib.util
    import types

    module_path = os.path.join(LAMBDAS_ROOT, "delivery", "alert_dispatcher", "outbreak_dispatch.py")
    spec = importlib.util.spec_from_file_location("outbreak_dispatch", module_path)
    module = types.ModuleType("outbreak_dispatch")
    module.__spec__ = spec
    module.__file__ = module_path

    with patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)

    # Register with mock context
    mock_sub_table = MagicMock()
    mock_ses = MagicMock()
    mock_sns = MagicMock()

    context = {
        "sub_table": mock_sub_table,
        "ses": mock_ses,
        "sns": mock_sns,
        "system": MOCK_SYSTEM,
        "api_base_url": "https://api.test.com",
        "dynamodb": MagicMock(),
    }
    module.register(context)

    module._test_mocks = {
        "sub_table": mock_sub_table,
        "ses": mock_ses,
        "sns": mock_sns,
    }

    return module


def _make_outbreak_event(state_key="texas"):
    return {
        "alert_type": "cdc_outbreak",
        "state_key": state_key,
        "disease_name": "Cyclosporiasis",
        "title": "Cyclosporiasis Outbreak",
        "affected_states": ["michigan", "ohio", "west virginia", "kentucky"],
        "new_states": ["kentucky"],
        "is_update": True,
        "case_count": 400,
        "cdc_link": "https://cdc.gov/test",
        "brief_result": {
            "Body": {
                "content": [{"text": "OUTBREAK BRIEF: Cyclosporiasis in 4 states..."}]
            }
        },
        "severity_result": {
            "Body": {
                "content": [{"text": '{"severity": "HIGH", "reasoning": "500+ cases across 4 states"}'}]
            }
        },
    }


def _make_subscriber(**overrides):
    base = {
        "county_fips": "48143",
        "subscription_id": "sub-001",
        "state": "texas",
        "status": "active",
        "verified_at": "2026-01-01T00:00:00",
        "delivery_preferences": {"channels": ["email"]},
        "contact_email": "officer@county.gov",
    }
    return {**base, **overrides}


class TestRegistration:
    """Test plugin registration."""

    def test_register_returns_cdc_outbreak_handler(self, outbreak_module):
        """register() returns dict with 'cdc_outbreak' key."""
        context = {
            "sub_table": MagicMock(),
            "ses": MagicMock(),
            "sns": MagicMock(),
            "system": MOCK_SYSTEM,
            "api_base_url": "https://test.com",
            "dynamodb": MagicMock(),
        }
        handlers = outbreak_module.register(context)
        assert "cdc_outbreak" in handlers
        assert callable(handlers["cdc_outbreak"])


class TestOutbreakDispatch:
    """Test outbreak alert delivery."""

    def test_dispatches_to_state_subscribers(self, outbreak_module):
        """Subscribers in the affected state receive email."""
        sub = _make_subscriber()
        # Set the module-level _sub_table mock directly
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}
        outbreak_module._ses = MagicMock()

        with patch("shared.token_utils.generate_unsubscribe_url", return_value="https://unsub.test"):
            result = outbreak_module.dispatch_outbreak_alert(_make_outbreak_event(), "cdc_outbreak")

        assert result["success"] is True
        assert result["total_dispatched"] == 1
        outbreak_module._ses.send_email.assert_called_once()

    def test_email_subject_contains_severity_and_disease(self, outbreak_module):
        """Email subject has severity and disease name."""
        sub = _make_subscriber()
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}
        outbreak_module._ses = MagicMock()

        with patch("shared.token_utils.generate_unsubscribe_url", return_value="https://unsub.test"):
            outbreak_module.dispatch_outbreak_alert(_make_outbreak_event(), "cdc_outbreak")

        call_args = outbreak_module._ses.send_email.call_args[1]
        subject = call_args["Message"]["Subject"]["Data"]
        assert "HIGH" in subject
        assert "Cyclosporiasis" in subject

    def test_missing_state_key_returns_error(self, outbreak_module):
        """Missing state_key returns error."""
        event = _make_outbreak_event()
        del event["state_key"]

        result = outbreak_module.dispatch_outbreak_alert(event, "cdc_outbreak")

        assert result["dispatched"] is False
        assert "Missing state_key" in result["error"]

    def test_no_subscribers_returns_skipped(self, outbreak_module):
        """No subscribers in state returns skipped."""
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": []}

        result = outbreak_module.dispatch_outbreak_alert(_make_outbreak_event(), "cdc_outbreak")

        assert result["total_dispatched"] == 0
        assert result["success"] is False


class TestStateSubscriberFiltering:
    """Test subscriber filtering logic."""

    def test_inactive_subscriber_excluded(self, outbreak_module):
        sub = _make_subscriber(status="cancelled")
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}

        result = outbreak_module._get_state_subscribers("texas")
        assert len(result) == 0

    def test_unverified_subscriber_excluded(self, outbreak_module):
        sub = _make_subscriber(verified_at=None)
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}

        result = outbreak_module._get_state_subscribers("texas")
        assert len(result) == 0

    def test_paused_subscriber_excluded(self, outbreak_module):
        sub = _make_subscriber(pause_until="2099-01-01T00:00:00")
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}

        result = outbreak_module._get_state_subscribers("texas")
        assert len(result) == 0

    def test_active_verified_subscriber_included(self, outbreak_module):
        sub = _make_subscriber()
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": [sub]}

        result = outbreak_module._get_state_subscribers("texas")
        assert len(result) == 1

    def test_queries_state_index_gsi(self, outbreak_module):
        outbreak_module._sub_table = MagicMock()
        outbreak_module._sub_table.query.return_value = {"Items": []}

        outbreak_module._get_state_subscribers("ohio")

        call_kwargs = outbreak_module._sub_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "state-index"
