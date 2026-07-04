"""Integration tests — Shortage Change Detector Lambda end-to-end flow.

Tests the change detector handler with mocked DynamoDB/S3/Step Functions to verify:
1. NEW shortages detected from empty state
2. WORSENING detected from status changes
3. RESOLVED detected from absent products
4. Step Functions invocations for NEW shortages
5. Circuit breaker stops alerts when threshold exceeded

Run with: pytest tests/integration/test_shortage_change_detection_flow.py -v
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone

# Ensure paths are set up for test imports
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "shared"))
sys.path.insert(0, os.path.join(ROOT, "lambdas"))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "prediction", "shortage_change_detector"))

# Load test fixtures
FIXTURES_PATH = os.path.join(ROOT, "tests", "data", "openfda_mock_responses.json")
with open(FIXTURES_PATH) as f:
    FIXTURES = json.load(f)


def _mock_therapeutic_config():
    """Return a minimal therapeutic category config for tests."""
    return {
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
                "fda_classification_mapping": ["*amoxicillin*", "*azithromycin*"],
            },
        ]
    }


def _mock_system_config():
    """Return mock system config."""
    return {
        "infrastructure": {
            "data_bucket_name_pattern": "healthsignals-data-test"
        }
    }


def _build_current_data_new_shortages():
    """Build normalized current shortage data with products not in previous state."""
    return [
        {
            "product_id": "PROD-001",
            "product_name": "Oseltamivir Phosphate Capsules, USP 75mg",
            "therapeutic_category": "antivirals",
            "supply_status": "DISCONTINUED",
            "reason_for_shortage": "Demand increase due to flu season",
            "estimated_resolution_date": "2024-03-15",
            "week_timestamp": "2024-W03",
        },
        {
            "product_id": "PROD-002",
            "product_name": "Amoxicillin Capsules, USP 500mg",
            "therapeutic_category": "antibiotics",
            "supply_status": "AVAILABLE",
            "reason_for_shortage": "Manufacturing delay",
            "estimated_resolution_date": "2024-02-28",
            "week_timestamp": "2024-W03",
        },
        {
            "product_id": "PROD-003",
            "product_name": "Metformin Hydrochloride Tablets",
            "therapeutic_category": "uncategorized",
            "supply_status": "DISCONTINUED",
            "reason_for_shortage": "Raw material shortage",
            "estimated_resolution_date": None,
            "week_timestamp": "2024-W03",
        },
    ]


def _build_worsening_data():
    """Build current data showing status worsening from AVAILABLE to DISCONTINUED."""
    return [
        {
            "product_id": "PROD-001",
            "product_name": "Oseltamivir Phosphate Capsules, USP 75mg",
            "therapeutic_category": "antivirals",
            "supply_status": "DISCONTINUED",
            "reason_for_shortage": "Demand increase due to flu season",
            "estimated_resolution_date": "2024-03-15",
            "week_timestamp": "2024-W03",
        },
    ]


def _build_previous_state_for_worsening():
    """Build previous state where PROD-001 was AVAILABLE (now DISCONTINUED = WORSENING)."""
    return {
        "PROD-001": {
            "product_id": "PROD-001",
            "product_name": "Oseltamivir Phosphate Capsules, USP 75mg",
            "therapeutic_category": "antivirals",
            "supply_status": "AVAILABLE",
            "reason_for_shortage": "Demand increase due to flu season",
            "estimated_resolution_date": "2024-03-15",
            "week_timestamp": "2024-W02",
            "shortage_status": "NEW",
        },
    }


def _load_handler_with_mocks(mock_s3_data, mock_ddb_state, mock_sfn=None, mock_cw=None):
    """Load the change detector handler module with mocked AWS clients.

    Args:
        mock_s3_data: Data that S3 get_object should return.
        mock_ddb_state: Dict mapping product_id → previous week state for DynamoDB scan.
        mock_sfn: Optional Step Functions mock. Default creates a MagicMock.
        mock_cw: Optional CloudWatch mock. Default creates a MagicMock.

    Returns:
        Tuple of (handler_module, mock_s3, mock_sfn, mock_state_table, mock_alerts_table)
    """
    mock_s3 = MagicMock()
    mock_s3_body = MagicMock()
    mock_s3_body.read.return_value = json.dumps(mock_s3_data).encode("utf-8")
    mock_s3.get_object.return_value = {"Body": mock_s3_body}

    if mock_sfn is None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:test:test-exec-001"
        }

    if mock_cw is None:
        mock_cw = MagicMock()

    # Mock DynamoDB tables
    mock_state_table = MagicMock()
    mock_alerts_table = MagicMock()

    # Build scan response from mock_ddb_state
    state_items = list(mock_ddb_state.values())
    mock_state_table.scan.return_value = {"Items": state_items}

    # No existing alerts (fresh state)
    mock_alerts_table.get_item.return_value = {}

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.side_effect = lambda name: {
        "healthsignals-drug-shortage-state": mock_state_table,
        "healthsignals-shortage-alerts": mock_alerts_table,
    }.get(name, MagicMock())

    # Patch and reload
    with patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()), \
         patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
         patch("boto3.client") as mock_boto_client, \
         patch("boto3.resource") as mock_boto_resource:

        def client_factory(service, **kwargs):
            if service == "s3":
                return mock_s3
            elif service == "cloudwatch":
                return mock_cw
            elif service == "stepfunctions":
                return mock_sfn
            return MagicMock()

        mock_boto_client.side_effect = client_factory
        mock_boto_resource.return_value = mock_dynamodb

        handler_dir = os.path.join(ROOT, "lambdas", "prediction", "shortage_change_detector")
        old_path = sys.path.copy()
        sys.path.insert(0, handler_dir)

        try:
            # Clear cached modules
            for mod_name in list(sys.modules.keys()):
                if "shortage_change_detector" in mod_name or mod_name == "handler":
                    del sys.modules[mod_name]

            import importlib
            import handler as detector_module
            importlib.reload(detector_module)

            # Override module-level clients and tables
            detector_module.s3 = mock_s3
            detector_module.sfn = mock_sfn
            detector_module.cloudwatch = mock_cw
            detector_module.state_table = mock_state_table
            detector_module.alerts_table = mock_alerts_table
            detector_module.STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"

            return detector_module, mock_s3, mock_sfn, mock_state_table, mock_alerts_table
        finally:
            sys.path = old_path


class TestNewShortagesDetected:
    """Test NEW shortages detected when DynamoDB state is empty."""

    def test_new_shortages_detected(self):
        """Load fixture data into mock S3, empty DynamoDB state → verify NEW changes."""
        current_data = _build_current_data_new_shortages()
        empty_state = {}  # No previous records

        detector, mock_s3, mock_sfn, mock_state_table, mock_alerts_table = \
            _load_handler_with_mocks(current_data, empty_state)

        event = {
            "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
            "week_timestamp": "2024-W03",
        }

        result = detector.lambda_handler(event, None)

        # Should detect NEW shortages for the 2 categorized products
        # PROD-003 is "uncategorized" so it gets filtered out
        changes = result["changes_detected"]
        assert changes["NEW"] == 2, f"Expected 2 NEW changes (excl uncategorized), got {changes['NEW']}"
        assert changes["RESOLVED"] == 0
        assert changes["UNCHANGED"] == 0
        assert result["circuit_breaker_activated"] is False


class TestWorseningDetected:
    """Test WORSENING detected when supply status degrades."""

    def test_worsening_detected(self):
        """Load fixture with changed status, seed DDB with previous state → verify WORSENING."""
        current_data = _build_worsening_data()
        previous_state = _build_previous_state_for_worsening()

        detector, mock_s3, mock_sfn, mock_state_table, mock_alerts_table = \
            _load_handler_with_mocks(current_data, previous_state)

        event = {
            "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
            "week_timestamp": "2024-W03",
        }

        result = detector.lambda_handler(event, None)

        # PROD-001 changed from AVAILABLE to DISCONTINUED → WORSENING
        changes = result["changes_detected"]
        assert changes["WORSENING"] == 1, f"Expected 1 WORSENING, got {changes['WORSENING']}"
        assert changes["NEW"] == 0
        assert result["circuit_breaker_activated"] is False


class TestResolvedDetected:
    """Test RESOLVED detected when previous products are absent from current data."""

    def test_resolved_detected(self):
        """Seed DDB with previous records, current data missing them → verify RESOLVED."""
        resolved_fixture = FIXTURES["scenario_resolved_shortages"]
        current_data = resolved_fixture["current_week_data"]

        # Build previous state dict from fixture
        previous_state = {}
        for record in resolved_fixture["previous_week_state"]:
            previous_state[record["product_id"]] = record

        detector, mock_s3, mock_sfn, mock_state_table, mock_alerts_table = \
            _load_handler_with_mocks(current_data, previous_state)

        event = {
            "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
            "week_timestamp": "2024-W03",
        }

        result = detector.lambda_handler(event, None)

        # PROD-004 was in previous state but not in current data → RESOLVED
        changes = result["changes_detected"]
        assert changes["RESOLVED"] >= 1, f"Expected at least 1 RESOLVED, got {changes['RESOLVED']}"
        assert result["circuit_breaker_activated"] is False


class TestStepFunctionsInvokedForNew:
    """Test Step Functions invoked for NEW shortages."""

    def test_step_functions_invoked_for_new(self):
        """Verify sfn.start_execution called for NEW shortages."""
        current_data = _build_current_data_new_shortages()
        empty_state = {}

        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:test:exec-001"
        }

        detector, mock_s3, _, mock_state_table, mock_alerts_table = \
            _load_handler_with_mocks(current_data, empty_state, mock_sfn=mock_sfn)

        event = {
            "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
            "week_timestamp": "2024-W03",
        }

        result = detector.lambda_handler(event, None)

        # Should have triggered Step Functions for each NEW shortage in monitored categories
        assert mock_sfn.start_execution.called, "Step Functions should be invoked for NEW shortages"
        assert result["alerts_triggered"] >= 1


class TestCircuitBreakerStopsAlerts:
    """Test circuit breaker activation when too many new shortages detected."""

    def test_circuit_breaker_stops_alerts(self):
        """Provide >20 new records in monitored categories → verify no SFN invocations."""
        # Create 25 new shortage records in antivirals category
        current_data = [
            {
                "product_id": f"PROD-CB-{i:03d}",
                "product_name": f"Oseltamivir Variant {i}",
                "therapeutic_category": "antivirals",
                "supply_status": "DISCONTINUED",
                "reason_for_shortage": "Test circuit breaker",
                "estimated_resolution_date": None,
                "week_timestamp": "2024-W03",
            }
            for i in range(25)
        ]

        empty_state = {}

        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:test:exec-cb"
        }

        detector, mock_s3, _, mock_state_table, mock_alerts_table = \
            _load_handler_with_mocks(current_data, empty_state, mock_sfn=mock_sfn)

        event = {
            "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
            "week_timestamp": "2024-W03",
        }

        result = detector.lambda_handler(event, None)

        # Circuit breaker should activate (25 > 20 threshold)
        assert result["circuit_breaker_activated"] is True
        # No SFN invocations when circuit breaker is active
        assert not mock_sfn.start_execution.called, \
            "Step Functions should NOT be invoked when circuit breaker is active"
        assert result["alerts_triggered"] == 0
