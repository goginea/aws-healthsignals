"""Unit tests for subscription verify and unsubscribe handlers."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

import sys
sys.path.insert(0, "lambdas/subscription/verify")
sys.path.insert(0, "lambdas/subscription/unsubscribe")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")

mock_system_config = {
    "dynamodb_tables": {"subscriptions": "healthsignals-subscriptions-test"},
    "delivery": {"ses_sender_email": "alerts@healthsignals.example.com"},
}


# ─────────────────────────────────────────────
# VERIFY HANDLER TESTS
# ─────────────────────────────────────────────

@pytest.fixture
def mock_verify_deps():
    with patch("config_loader.get_system_config", return_value=mock_system_config), \
         patch("boto3.resource"), \
         patch("boto3.client"):
        yield


def import_verify():
    import importlib
    import handler as vh
    return vh


class TestVerifyHandler:
    """Tests for the verify handler."""

    def _make_event(self, token="valid.token"):
        return {"queryStringParameters": {"token": token}}

    def _active_sub(self):
        return {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "pending_verification",
            "contact_email": "officer@county.gov",
            "contact_name": "Dr. Smith",
            "county_name": "Erath County",
            "diseases": ["influenza", "rsv"],
            "delivery_preferences": {"channels": ["email"]},
        }

    @pytest.fixture(autouse=True)
    def setup(self, mock_verify_deps):
        pass

    @patch("sys.path", sys.path)
    def test_missing_token_returns_400(self):
        sys.path.insert(0, "lambdas/subscription/verify")
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"):
            import importlib
            import handler as vh
            importlib.reload(vh)

            event = {"queryStringParameters": {}}
            result = vh.lambda_handler(event, None)
            assert result["statusCode"] == 400

    @patch("sys.path", sys.path)
    def test_invalid_token_returns_401(self):
        sys.path.insert(0, "lambdas/subscription/verify")
        from token_utils import TokenError
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", side_effect=TokenError("expired")):
            import importlib
            import handler as vh
            importlib.reload(vh)

            result = vh.lambda_handler(self._make_event("bad.token"), None)
            assert result["statusCode"] == 401

    @patch("sys.path", sys.path)
    def test_already_active_returns_200(self):
        sys.path.insert(0, "lambdas/subscription/verify")
        active_sub = {**self._active_sub(), "status": "active", "verified_at": "2026-01-01"}
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-001", "purpose": "verification"}):
            import importlib
            import handler as vh
            importlib.reload(vh)
            vh.table.get_item = MagicMock(return_value={"Item": active_sub})

            result = vh.lambda_handler(self._make_event(), None)
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "already verified" in body["message"].lower()

    @patch("sys.path", sys.path)
    def test_not_found_returns_404(self):
        sys.path.insert(0, "lambdas/subscription/verify")
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-999", "purpose": "verification"}):
            import importlib
            import handler as vh
            importlib.reload(vh)
            vh.table.get_item = MagicMock(return_value={})  # No item

            result = vh.lambda_handler(self._make_event(), None)
            assert result["statusCode"] == 404

    @patch("sys.path", sys.path)
    def test_successful_verification_activates_subscription(self):
        sys.path.insert(0, "lambdas/subscription/verify")
        sub = self._active_sub()
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-001", "purpose": "verification"}):
            import importlib
            import handler as vh
            importlib.reload(vh)
            vh.table.get_item = MagicMock(return_value={"Item": sub})
            vh.table.update_item = MagicMock(return_value={})
            vh._send_welcome_email = MagicMock()

            result = vh.lambda_handler(self._make_event(), None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["status"] == "active"

            update_call = vh.table.update_item.call_args[1]
            assert ":status" in update_call["ExpressionAttributeValues"]
            assert update_call["ExpressionAttributeValues"][":status"] == "active"


# ─────────────────────────────────────────────
# UNSUBSCRIBE HANDLER TESTS
# ─────────────────────────────────────────────

class TestUnsubscribeHandler:
    """Tests for the unsubscribe handler."""

    @pytest.fixture(autouse=True)
    def setup(self):
        sys.path.insert(0, "lambdas/subscription/unsubscribe")

    def _get_event(self, token="valid.unsubscribe.token"):
        return {"httpMethod": "GET", "queryStringParameters": {"token": token}}

    def _post_event(self, body: dict):
        return {"httpMethod": "POST", "body": json.dumps(body)}

    def _active_sub(self):
        return {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "contact_email": "officer@county.gov",
            "contact_name": "Dr. Smith",
            "county_name": "Erath County",
        }

    @patch("sys.path", sys.path)
    def test_get_missing_token_returns_400(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"):
            import importlib, handler as uh
            importlib.reload(uh)

            event = {"httpMethod": "GET", "queryStringParameters": {}}
            result = uh.lambda_handler(event, None)
            assert result["statusCode"] == 400

    @patch("sys.path", sys.path)
    def test_get_invalid_token_returns_401(self):
        from token_utils import TokenError
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", side_effect=TokenError("invalid")):
            import importlib, handler as uh
            importlib.reload(uh)

            result = uh.lambda_handler(self._get_event("bad.token"), None)
            assert result["statusCode"] == 401

    @patch("sys.path", sys.path)
    def test_already_inactive_returns_200(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-001", "purpose": "unsubscribe"}):
            import importlib, handler as uh
            importlib.reload(uh)
            uh.table.get_item = MagicMock(return_value={"Item": {**self._active_sub(), "status": "inactive"}})

            result = uh.lambda_handler(self._get_event(), None)
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "already" in body["message"].lower()

    @patch("sys.path", sys.path)
    def test_get_token_unsubscribe_succeeds(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-001", "purpose": "unsubscribe"}):
            import importlib, handler as uh
            importlib.reload(uh)
            uh.table.get_item = MagicMock(return_value={"Item": self._active_sub()})
            uh.table.update_item = MagicMock(return_value={})
            uh._send_unsubscribe_confirmation = MagicMock()

            result = uh.lambda_handler(self._get_event(), None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "unsubscribed" in body["message"].lower()

            update_call = uh.table.update_item.call_args[1]
            assert update_call["ExpressionAttributeValues"][":status"] == "inactive"

    @patch("sys.path", sys.path)
    def test_post_unsubscribe_succeeds(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"):
            import importlib, handler as uh
            importlib.reload(uh)
            uh.table.get_item = MagicMock(return_value={"Item": self._active_sub()})
            uh.table.update_item = MagicMock(return_value={})
            uh._send_unsubscribe_confirmation = MagicMock()

            event = self._post_event({"county_fips": "48143", "subscription_id": "sub-001"})
            result = uh.lambda_handler(event, None)

            assert result["statusCode"] == 200

    @patch("sys.path", sys.path)
    def test_post_missing_fields_returns_400(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"):
            import importlib, handler as uh
            importlib.reload(uh)

            event = self._post_event({"county_fips": "48143"})  # Missing subscription_id
            result = uh.lambda_handler(event, None)
            assert result["statusCode"] == 400

    @patch("sys.path", sys.path)
    def test_not_found_returns_404(self):
        with patch("config_loader.get_system_config", return_value=mock_system_config), \
             patch("boto3.resource"), \
             patch("boto3.client"), \
             patch("token_utils.validate_token", return_value={"fips": "48143", "sub": "sub-999", "purpose": "unsubscribe"}):
            import importlib, handler as uh
            importlib.reload(uh)
            uh.table.get_item = MagicMock(return_value={})

            result = uh.lambda_handler(self._get_event(), None)
            assert result["statusCode"] == 404
