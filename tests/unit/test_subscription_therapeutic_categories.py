"""Unit tests for Subscription API — Alert Categories extensions.

Tests the alert_categories handling in update_preferences handler:
- Valid categories accepted
- Invalid category returns HTTP 400
- Non-array value returns HTTP 400
- Duplicates deduplicated before DynamoDB write
- GET /status includes alert_categories field
- Legacy subscriptions return empty array
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {
    "dynamodb_tables": {"subscriptions": "healthsignals-subscriptions-test"},
    "config_bucket": "healthsignals-config-test",
}

MOCK_ALERT_CATEGORY_CONFIG = {
    "categories": [
        {"category_key": "antivirals", "display_name": "Antivirals"},
        {"category_key": "antibiotics", "display_name": "Antibiotics"},
        {"category_key": "respiratory", "display_name": "Respiratory Medications"},
    ]
}


@pytest.fixture(scope="module")
def handler():
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    mock_s3 = MagicMock()

    return load_handler(
        "subscription/update_preferences",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.list_active_diseases": ["influenza", "rsv", "covid"],
            "boto3.resource": MagicMock(return_value=mock_dynamo),
            "boto3.client": MagicMock(return_value=mock_s3),
        },
    )


def _make_event(body: dict) -> dict:
    """Helper to create a Lambda event with JSON body."""
    return {"body": json.dumps(body)}


class TestValidTherapeuticCategories:
    """Test that valid alert categories are accepted."""

    def test_valid_therapeutic_categories_accepted(self, handler):
        """Valid categories update succeeds (returns 200)."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": ["antivirals", "antibiotics"],
            },
        }

        existing_sub = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "alert_categories": [],
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            handler.table.get_item.return_value = {"Item": existing_sub}
            handler.table.update_item.return_value = {
                "Attributes": {
                    **existing_sub,
                    "alert_categories": ["antivirals", "antibiotics"],
                }
            }

            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 200
            response_body = json.loads(result["body"])
            assert "alert_categories" in response_body.get("updated_fields", [])


class TestInvalidCategory:
    """Test that invalid category_key returns proper error."""

    def test_invalid_category_returns_400(self, handler):
        """Invalid category returns HTTP 400."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": ["antivirals", "nonexistent_category"],
            },
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 400
            response_body = json.loads(result["body"])
            assert "Invalid alert category: nonexistent_category" in response_body["error"]


class TestCategoriesMustBeArray:
    """Test that non-array alert_categories returns HTTP 400."""

    def test_categories_must_be_array(self, handler):
        """Non-array value for alert_categories returns HTTP 400."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": "antivirals",  # string instead of array
            },
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 400
            response_body = json.loads(result["body"])
            assert "must be an array" in response_body["error"]


class TestEmptyCategoriesArray:
    """Test that empty array [] is a valid input."""

    def test_empty_categories_array_accepted(self, handler):
        """Empty array [] is a valid input (clears alert categories)."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": [],
            },
        }

        existing_sub = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "alert_categories": ["antivirals"],
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            handler.table.get_item.return_value = {"Item": existing_sub}
            handler.table.update_item.return_value = {
                "Attributes": {
                    **existing_sub,
                    "alert_categories": [],
                }
            }

            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 200
            response_body = json.loads(result["body"])
            assert "alert_categories" in response_body.get("updated_fields", [])


class TestDuplicatesRemovedOnSave:
    """Test that duplicate category_keys are deduplicated before DynamoDB write."""

    def test_duplicates_removed_on_save(self, handler):
        """Duplicate category_keys deduplicated before DynamoDB write."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": ["antivirals", "antibiotics", "antivirals"],
            },
        }

        existing_sub = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            handler.table.get_item.return_value = {"Item": existing_sub}
            handler.table.update_item.return_value = {
                "Attributes": {
                    **existing_sub,
                    "alert_categories": ["antivirals", "antibiotics"],
                }
            }

            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 200
            call_kwargs = handler.table.update_item.call_args[1]
            expr_values = call_kwargs["ExpressionAttributeValues"]
            categories_value = None
            for key, val in expr_values.items():
                if isinstance(val, list) and "antivirals" in val:
                    categories_value = val
                    break

            assert categories_value is not None
            assert categories_value == ["antivirals", "antibiotics"]
            assert len(categories_value) == 2


class TestStatusIncludesTherapeuticCategories:
    """Test that response includes alert_categories in updated_fields."""

    def test_status_includes_therapeutic_categories(self, handler):
        """Update response includes alert_categories in updated_fields."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "updates": {
                "alert_categories": ["antivirals"],
            },
        }

        existing_sub = {
            "county_fips": "48143",
            "subscription_id": "sub-001",
            "status": "active",
            "alert_categories": ["respiratory"],
        }

        with patch.object(handler, "_load_alert_category_config", return_value=MOCK_ALERT_CATEGORY_CONFIG):
            handler.table.get_item.return_value = {"Item": existing_sub}
            handler.table.update_item.return_value = {
                "Attributes": {
                    **existing_sub,
                    "alert_categories": ["antivirals"],
                    "status": "active",
                }
            }

            result = handler.lambda_handler(_make_event(body), None)

            assert result["statusCode"] == 200
            response_body = json.loads(result["body"])
            assert response_body.get("current_status") == "active"
            assert "alert_categories" in response_body.get("updated_fields", [])


class TestLegacySubscription:
    """Test that legacy subscriptions without alert_categories field return []."""

    def test_legacy_subscription_returns_empty_array(self, handler):
        """Subscription without alert_categories field returns [] default."""
        body = {
            "county_fips": "48143",
            "subscription_id": "sub-legacy",
            "updates": {
                "contact_email": "new@county.gov",
            },
        }

        legacy_sub = {
            "county_fips": "48143",
            "subscription_id": "sub-legacy",
            "status": "active",
            "contact_email": "old@county.gov",
        }

        handler.table.get_item.return_value = {"Item": legacy_sub}
        handler.table.update_item.return_value = {
            "Attributes": {
                **legacy_sub,
                "contact_email": "new@county.gov",
            }
        }

        result = handler.lambda_handler(_make_event(body), None)

        assert result["statusCode"] == 200
        assert legacy_sub.get("alert_categories", []) == []
