"""Unit tests for shortage change detector Lambda.

Tests the change classification logic (NEW, WORSENING, RESOLVED, UNCHANGED),
therapeutic category filtering, circuit breaker, and idempotency checks.
"""
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


# ── Mock configs ─────────────────────────────────────────────────────────

MOCK_SYSTEM = {
    "infrastructure": {
        "data_bucket_name_pattern": "healthsignals-data-test"
    },
    "dynamodb_tables": {
        "shortage_state": "healthsignals-drug-shortage-state-test",
        "shortage_alerts": "healthsignals-shortage-alerts-test",
    },
}

MOCK_THERAPEUTIC_CONFIG = {
    "categories": [
        {
            "category_key": "antivirals",
            "display_name": "Antivirals",
            "priority_level": "HIGH",
            "relevant_diseases": ["influenza", "covid"],
            "fda_classification_mapping": ["*oseltamivir*", "*tamiflu*"],
        },
        {
            "category_key": "antibiotics",
            "display_name": "Antibiotics",
            "priority_level": "HIGH",
            "relevant_diseases": ["influenza", "covid", "rsv"],
            "fda_classification_mapping": ["*amoxicillin*"],
        },
    ]
}


@pytest.fixture(scope="module")
def handler():
    """Load the shortage change detector handler with mocked dependencies."""
    mock_dynamodb = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    return load_handler(
        "prediction/shortage_change_detector",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader._load_config": MOCK_THERAPEUTIC_CONFIG,
            "boto3.client": MagicMock(),
            "boto3.resource": mock_dynamodb,
        },
    )


# ── Tests: classify_changes (Task 6.2) ──────────────────────────────────


class TestClassifyNew:
    """Test NEW classification for product_id not in previous state."""

    def test_classify_new(self, handler):
        """product_id in current data but NOT in previous state → NEW."""
        current_data = [
            {
                "product_id": "NEW-001",
                "product_name": "Oseltamivir Capsules",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Demand increase",
                "therapeutic_category": "antivirals",
            }
        ]
        previous_state = {}  # Empty previous state

        changes = handler.classify_changes(current_data, previous_state)

        assert len(changes["NEW"]) == 1
        assert changes["NEW"][0]["product_id"] == "NEW-001"
        assert changes["NEW"][0]["shortage_status"] == "NEW"


class TestClassifyWorsening:
    """Test WORSENING classification rules."""

    def test_classify_worsening_status_change(self, handler):
        """supply_status AVAILABLE → DISCONTINUED → WORSENING."""
        current_data = [
            {
                "product_id": "WORSE-001",
                "product_name": "Tamiflu",
                "supply_status": "DISCONTINUED",
                "reason_for_shortage": "Manufacturing delay",
                "therapeutic_category": "antivirals",
            }
        ]
        previous_state = {
            "WORSE-001": {
                "product_id": "WORSE-001",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Manufacturing delay",
                "therapeutic_category": "antivirals",
            }
        }

        changes = handler.classify_changes(current_data, previous_state)

        assert len(changes["WORSENING"]) == 1
        assert changes["WORSENING"][0]["product_id"] == "WORSE-001"
        assert changes["WORSENING"][0]["shortage_status"] == "WORSENING"
        assert changes["WORSENING"][0]["previous_supply_status"] == "AVAILABLE"

    def test_classify_worsening_reason_change(self, handler):
        """reason_for_shortage changed → WORSENING."""
        current_data = [
            {
                "product_id": "WORSE-002",
                "product_name": "Amoxicillin",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Raw material shortage",
                "therapeutic_category": "antibiotics",
            }
        ]
        previous_state = {
            "WORSE-002": {
                "product_id": "WORSE-002",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Demand increase",
                "therapeutic_category": "antibiotics",
            }
        }

        changes = handler.classify_changes(current_data, previous_state)

        assert len(changes["WORSENING"]) == 1
        assert changes["WORSENING"][0]["product_id"] == "WORSE-002"
        assert changes["WORSENING"][0]["shortage_status"] == "WORSENING"


class TestClassifyResolved:
    """Test RESOLVED classification for product_id in previous but not current."""

    def test_classify_resolved(self, handler):
        """product_id in previous state but NOT in current → RESOLVED."""
        current_data = []  # Product no longer in current data
        previous_state = {
            "RESOLVED-001": {
                "product_id": "RESOLVED-001",
                "product_name": "Oseltamivir",
                "supply_status": "DISCONTINUED",
                "reason_for_shortage": "Supply issue",
                "therapeutic_category": "antivirals",
            }
        }

        changes = handler.classify_changes(current_data, previous_state)

        assert len(changes["RESOLVED"]) == 1
        assert changes["RESOLVED"][0]["product_id"] == "RESOLVED-001"
        assert changes["RESOLVED"][0]["shortage_status"] == "RESOLVED"


class TestClassifyUnchanged:
    """Test UNCHANGED classification when all fields match."""

    def test_classify_unchanged(self, handler):
        """All fields match → UNCHANGED."""
        current_data = [
            {
                "product_id": "SAME-001",
                "product_name": "Tamiflu",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Demand increase",
                "therapeutic_category": "antivirals",
            }
        ]
        previous_state = {
            "SAME-001": {
                "product_id": "SAME-001",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Demand increase",
                "therapeutic_category": "antivirals",
            }
        }

        changes = handler.classify_changes(current_data, previous_state)

        assert len(changes["UNCHANGED"]) == 1
        assert changes["UNCHANGED"][0]["product_id"] == "SAME-001"
        assert changes["UNCHANGED"][0]["shortage_status"] == "UNCHANGED"


class TestTherapeuticCategoryFiltering:
    """Test filtering excludes uncategorized records."""

    def test_filter_excludes_uncategorized(self, handler):
        """Records with 'uncategorized' category excluded from results."""
        changes = {
            "NEW": [
                {
                    "product_id": "FILT-001",
                    "therapeutic_category": "antivirals",
                    "shortage_status": "NEW",
                },
                {
                    "product_id": "FILT-002",
                    "therapeutic_category": "uncategorized",
                    "shortage_status": "NEW",
                },
            ],
            "WORSENING": [],
            "RESOLVED": [],
            "UNCHANGED": [],
        }
        monitored_categories = {"antivirals", "antibiotics", "respiratory"}

        filtered = handler.filter_by_therapeutic_categories(changes, monitored_categories)

        assert len(filtered["NEW"]) == 1
        assert filtered["NEW"][0]["product_id"] == "FILT-001"


class TestCircuitBreaker:
    """Test circuit breaker activation threshold."""

    def test_circuit_breaker_threshold(self, handler):
        """NEW+WORSENING > 20 means circuit breaker should activate."""
        # Generate 15 NEW + 10 WORSENING = 25 total > 20
        new_records = [
            {
                "product_id": f"CB-NEW-{i}",
                "product_name": f"Drug {i}",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Demand",
                "therapeutic_category": "antivirals",
            }
            for i in range(15)
        ]
        worsening_records = [
            {
                "product_id": f"CB-WORSE-{i}",
                "product_name": f"Drug {i}",
                "supply_status": "DISCONTINUED",
                "reason_for_shortage": "New reason",
                "therapeutic_category": "antibiotics",
            }
            for i in range(10)
        ]

        # Build previous state for worsening records
        previous_state = {
            f"CB-WORSE-{i}": {
                "product_id": f"CB-WORSE-{i}",
                "supply_status": "AVAILABLE",
                "reason_for_shortage": "Old reason",
                "therapeutic_category": "antibiotics",
            }
            for i in range(10)
        }

        current_data = new_records + worsening_records
        changes = handler.classify_changes(current_data, previous_state)

        alertable_count = len(changes["NEW"]) + len(changes["WORSENING"])
        assert alertable_count > handler.CIRCUIT_BREAKER_THRESHOLD


# ── Tests: Idempotency logic (Task 6.3) ─────────────────────────────────


class TestIdempotency:
    """Test alert idempotency checking logic."""

    def test_alert_skipped_when_already_sent(self, handler):
        """Record exists with status SENT → skip alert generation."""
        mock_response = {
            "Item": {
                "product_id": "IDEM-001",
                "week_timestamp": "2024-W03",
                "alert_generated": "SENT",
                "retry_count": 0,
            }
        }

        with patch.object(handler.alerts_table, "get_item", return_value=mock_response):
            result = handler.is_alert_already_sent("IDEM-001", "2024-W03")
            assert result is True

    def test_alert_allowed_when_no_record(self, handler):
        """No record exists for product_id/week → allow alert generation."""
        mock_response = {}  # No "Item" key means record doesn't exist

        with patch.object(handler.alerts_table, "get_item", return_value=mock_response):
            result = handler.is_alert_already_sent("IDEM-NEW", "2024-W03")
            assert result is False

    def test_alert_retried_when_failed(self, handler):
        """Record with FAILED and retry_count < 3 → allow retry."""
        mock_response = {
            "Item": {
                "product_id": "IDEM-002",
                "week_timestamp": "2024-W03",
                "alert_generated": "FAILED",
                "retry_count": 1,
            }
        }

        with patch.object(handler.alerts_table, "get_item", return_value=mock_response):
            result = handler.is_alert_already_sent("IDEM-002", "2024-W03")
            assert result is False

    def test_alert_skipped_when_max_retries(self, handler):
        """Record with FAILED and retry_count >= 3 → skip."""
        mock_response = {
            "Item": {
                "product_id": "IDEM-003",
                "week_timestamp": "2024-W03",
                "alert_generated": "FAILED",
                "retry_count": 3,
            }
        }

        with patch.object(handler.alerts_table, "get_item", return_value=mock_response):
            result = handler.is_alert_already_sent("IDEM-003", "2024-W03")
            assert result is True
