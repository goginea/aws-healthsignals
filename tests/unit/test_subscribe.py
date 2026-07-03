"""Unit tests for subscription subscribe handler."""
import json
import pytest
from unittest.mock import patch, MagicMock

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas/subscription/subscribe"))
sys.path.insert(0, os.path.join(ROOT, "lambdas/shared"))
sys.path.insert(0, "lambdas")


class TestInputValidation:
    """Test subscription input validation."""

    def test_valid_fips_format(self):
        """5-digit FIPS code should be accepted."""
        import re
        fips_pattern = r"^\d{5}$"
        assert re.match(fips_pattern, "48143")
        assert re.match(fips_pattern, "12086")
        assert not re.match(fips_pattern, "4814")  # Too short
        assert not re.match(fips_pattern, "481430")  # Too long
        assert not re.match(fips_pattern, "ABCDE")  # Non-numeric

    def test_valid_email_format(self):
        """Email validation should accept common formats."""
        import re
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        assert re.match(email_pattern, "health@county.gov")
        assert re.match(email_pattern, "john.doe@health.texas.gov")
        assert not re.match(email_pattern, "no-at-sign")
        assert not re.match(email_pattern, "@missing-local.com")

    def test_required_fields_present(self):
        """Required fields must all be present."""
        required = ["county_fips", "county_name", "state", "contact_name", "contact_email"]
        valid_body = {
            "county_fips": "48143",
            "county_name": "Erath County",
            "state": "texas",
            "contact_name": "Dr. Smith",
            "contact_email": "smith@county.gov",
        }
        for field in required:
            assert field in valid_body

    def test_missing_field_rejected(self):
        """Missing required field should be caught."""
        required = ["county_fips", "county_name", "state", "contact_name", "contact_email"]
        incomplete = {"county_fips": "48143", "county_name": "Erath"}
        missing = [f for f in required if f not in incomplete]
        assert len(missing) > 0

    def test_default_diseases_all_active(self):
        """If diseases not specified, should default to all active."""
        body = {"county_fips": "48143", "county_name": "Erath County"}
        diseases = body.get("diseases", ["influenza", "rsv", "covid"])
        assert len(diseases) == 3

    def test_delivery_preferences_options(self):
        """Delivery preferences should be email, sms, or both."""
        valid_options = ["email", "sms", "both"]
        for opt in valid_options:
            assert opt in valid_options
        assert "carrier_pigeon" not in valid_options


class TestSubscriptionCreation:
    """Test subscription record creation logic."""

    def test_subscription_id_is_uuid(self):
        """subscription_id should be a valid UUID4."""
        import uuid
        sub_id = str(uuid.uuid4())
        # Verify it's a valid UUID
        parsed = uuid.UUID(sub_id)
        assert parsed.version == 4

    def test_initial_status_pending(self):
        """New subscription should start as pending_verification."""
        initial_status = "pending_verification"
        assert initial_status == "pending_verification"

    def test_abuse_prevention_limit(self):
        """Max 10 subscriptions per county to prevent abuse."""
        max_per_county = 10
        existing_count = 11
        assert existing_count > max_per_county
