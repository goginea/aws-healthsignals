"""Unit tests for Subscription subscribe Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {
    "dynamodb_tables": {"subscriptions": "healthsignals-subscriptions-test"},
    "subscription": {"max_per_county": 10, "verification_token_expiry_hours": 72},
    "delivery": {"ses_sender_email": "alerts@healthsignals.example.com"},
}


@pytest.fixture(scope="module")
def handler():
    return load_handler(
        "subscription/subscribe",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.list_active_states": [{"state_key": "texas"}],
            "shared.config_loader.list_active_diseases": [{"disease_key": "influenza"}, {"disease_key": "rsv"}],
            "boto3.resource": MagicMock(),
            "boto3.client": MagicMock(),
        },
    )


class TestSubscribe:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_missing_required_fields(self, handler):
        """Missing county_fips should return 400."""
        event = {"body": json.dumps({"contact_email": "test@test.com"})}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_invalid_email(self, handler):
        """Invalid email format should return 400."""
        event = {"body": json.dumps({
            "county_fips": "48143",
            "county_name": "Erath County",
            "state": "texas",
            "contact_name": "Dr. Test",
            "contact_email": "not-an-email",
        })}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_invalid_fips(self, handler):
        """Invalid FIPS format should return 400."""
        event = {"body": json.dumps({
            "county_fips": "ABC",
            "county_name": "Test",
            "state": "texas",
            "contact_name": "Dr. Test",
            "contact_email": "test@test.com",
        })}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_valid_subscription(self, handler):
        """Valid input should create subscription."""
        with patch.object(handler, "_count_active_subscriptions", return_value=0), \
             patch.object(handler, "_send_verification_email"):
            event = {"body": json.dumps({
                "county_fips": "48143",
                "county_name": "Erath County",
                "state": "texas",
                "contact_name": "Dr. Jane Smith",
                "contact_email": "jane@erathcounty.gov",
                "diseases": ["influenza"],
            })}
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 201 or result["statusCode"] == 200

    def test_json_parse_error(self, handler):
        """Invalid JSON body should return 400."""
        event = {"body": "not json"}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400
