"""Integration tests — Combined Disease+Shortage Signal end-to-end flow.

Tests the Pipeline Coordinator's combined signal logic with mocked AWS services:
1. Combined signal generation when shortages exist for a disease
2. Standard alert when no shortages exist
3. Shortage context includes only products from relevant categories

Run with: pytest tests/integration/test_combined_signal_flow.py -v
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime

# Ensure paths are set up for test imports
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "shared"))
sys.path.insert(0, os.path.join(ROOT, "lambdas"))
sys.path.insert(0, os.path.join(ROOT, "lambdas", "orchestration", "pipeline_coordinator"))


def _mock_therapeutic_config():
    """Return therapeutic category config with disease mappings."""
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
            {
                "category_key": "respiratory",
                "display_name": "Respiratory Medications",
                "priority_level": "MEDIUM",
                "relevant_diseases": ["rsv", "influenza", "covid"],
                "fda_classification_mapping": ["*albuterol*", "*budesonide*"],
            },
        ]
    }


def _mock_system_config():
    """Return mock system config."""
    return {
        "infrastructure": {
            "data_bucket_name_pattern": "healthsignals-data-test"
        },
        "dynamodb_tables": {
            "alert_state": "healthsignals-alert-state"
        },
    }


def _mock_state_config():
    """Return mock state config for Texas."""
    return {
        "state_key": "texas",
        "sentinel_metros": {
            "26420": {
                "name": "Houston",
                "primary_county_fips": "48201",
                "county_fips": ["48201"],
            }
        },
    }


def _mock_disease_config():
    """Return mock disease config for influenza."""
    return {
        "disease_key": "influenza",
        "data_sources": {
            "delphi": {
                "data_source": "nssp",
                "signal": "pct_ed_visits_influenza",
            }
        },
    }


def _shortage_state_items_with_new():
    """Items that DynamoDB GSI query returns for 'antivirals' category with NEW status."""
    return [
        {
            "product_id": "PROD-001",
            "product_name": "Oseltamivir Phosphate Capsules, USP 75mg",
            "therapeutic_category": "antivirals",
            "supply_status": "DISCONTINUED",
            "shortage_status": "NEW",
            "reason_for_shortage": "Demand increase due to flu season",
            "estimated_resolution_date": "2024-03-15",
            "week_timestamp": "2024-W03",
        },
    ]


def _shortage_state_items_antibiotics():
    """Items for 'antibiotics' category with WORSENING status."""
    return [
        {
            "product_id": "PROD-002",
            "product_name": "Amoxicillin Capsules, USP 500mg",
            "therapeutic_category": "antibiotics",
            "supply_status": "DISCONTINUED",
            "shortage_status": "WORSENING",
            "reason_for_shortage": "Manufacturing delay",
            "estimated_resolution_date": "2024-02-28",
            "week_timestamp": "2024-W03",
        },
    ]


def _setup_pipeline_coordinator(shortage_items_by_category=None):
    """Load the Pipeline Coordinator handler with mocked dependencies.

    Args:
        shortage_items_by_category: Dict mapping category_key → list of DDB items.
            If None, DynamoDB queries return empty results.

    Returns:
        Tuple of (handler_module, mock_sfn_client, mock_lambda_client, mock_shortage_table)
    """
    if shortage_items_by_category is None:
        shortage_items_by_category = {}

    mock_sfn = MagicMock()
    mock_sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123456789012:execution:test:combined-001",
        "startDate": datetime(2024, 1, 15, 6, 5, 12),
    }

    mock_lambda = MagicMock()
    mock_s3 = MagicMock()

    # Mock shortage state DynamoDB table
    mock_shortage_table = MagicMock()

    def query_side_effect(**kwargs):
        """Return items based on the KeyConditionExpression.

        Uses boto3's ConditionExpressionBuilder to extract the actual
        category value from the Key condition expression.
        """
        from boto3.dynamodb.conditions import ConditionExpressionBuilder

        key_expr = kwargs.get("KeyConditionExpression", None)
        if key_expr is None:
            return {"Items": []}

        try:
            builder = ConditionExpressionBuilder()
            built = builder.build_expression(key_expr, is_key_condition=True)
            # Extract category from attribute value placeholders
            for placeholder, value in built.attribute_value_placeholders.items():
                if isinstance(value, str) and value in shortage_items_by_category:
                    return {"Items": shortage_items_by_category[value]}
        except Exception:
            pass

        return {"Items": []}

    mock_shortage_table.query.side_effect = query_side_effect

    # Mock alert_state table and pipeline_runs table
    mock_alert_state_table = MagicMock()
    mock_alert_state_table.get_item.return_value = {}

    mock_pipeline_runs_table = MagicMock()

    mock_dynamodb = MagicMock()

    def table_factory(name):
        if name == "healthsignals-drug-shortage-state":
            return mock_shortage_table
        elif name == "healthsignals-alert-state":
            return mock_alert_state_table
        elif name == "healthsignals-pipeline-runs":
            return mock_pipeline_runs_table
        return MagicMock()

    mock_dynamodb.Table.side_effect = table_factory

    # Setup leader detection response (simulates a detected outbreak)
    leader_detection_response = {
        "detected": True,
        "new_alert": True,
        "leader": {
            "msa_code": "26420",
            "metro_name": "Houston",
            "value": 8.5,
        },
    }

    # Setup geographic affinity response
    geo_affinity_response = {
        "affected_counties": [
            {
                "county_fips": "48143",
                "county_name": "Erath County",
                "affinity_weight": 0.75,
                "alert_contacts": [{"email": "test@county.gov"}],
            }
        ]
    }

    # Setup timing estimation response
    timing_response = {
        "estimated_lag_weeks": 3,
        "severity_multiplier": 1.8,
        "confidence": 0.72,
        "seasons_calibrated": 4,
        "warning_window_weeks": 3,
        "cdc_activity_level": "high",
    }

    def lambda_invoke_side_effect(**kwargs):
        """Route Lambda invocations to mock responses."""
        function_name = kwargs.get("FunctionName", "")
        if "leader-detection" in function_name:
            payload = json.dumps(leader_detection_response)
        elif "geographic-affinity" in function_name:
            payload = json.dumps(geo_affinity_response)
        elif "timing-estimation" in function_name:
            payload = json.dumps(timing_response)
        else:
            payload = json.dumps({"statusCode": 200})

        mock_payload = MagicMock()
        mock_payload.read.return_value = payload.encode("utf-8")
        return {"Payload": mock_payload}

    mock_lambda.invoke.side_effect = lambda_invoke_side_effect

    # Mock S3 list_objects for metro signals
    mock_s3.list_objects_v2.return_value = {
        "CommonPrefixes": [{"Prefix": "raw/delphi/nssp/pct_ed_visits_influenza/2024/"}]
    }
    mock_s3_body = MagicMock()
    mock_s3_body.read.return_value = json.dumps({
        "epidata": [
            {"time_value": 20240115, "value": 8.5},
            {"time_value": 20240108, "value": 7.2},
            {"time_value": 20240101, "value": 6.1},
        ]
    }).encode("utf-8")
    mock_s3.get_object.return_value = {"Body": mock_s3_body}

    with patch("shared.config_loader.get_system_config", return_value=_mock_system_config()), \
         patch("shared.config_loader.get_state_config", return_value=_mock_state_config()), \
         patch("shared.config_loader.get_disease_config", return_value=_mock_disease_config()), \
         patch("shared.config_loader.list_active_states", return_value=[{"state_key": "texas"}]), \
         patch("shared.config_loader.list_active_diseases", return_value=[{"disease_key": "influenza"}]), \
         patch("shared.config_loader.get_all_sentinel_metros", return_value={"26420": {"state_key": "texas"}}), \
         patch("shared.config_loader._load_config", return_value=_mock_therapeutic_config()), \
         patch("boto3.client") as mock_boto_client, \
         patch("boto3.resource") as mock_boto_resource:

        def client_factory(service, **kwargs):
            if service == "lambda":
                return mock_lambda
            elif service == "stepfunctions":
                return mock_sfn
            elif service == "s3":
                return mock_s3
            return MagicMock()

        mock_boto_client.side_effect = client_factory
        mock_boto_resource.return_value = mock_dynamodb

        handler_dir = os.path.join(ROOT, "lambdas", "orchestration", "pipeline_coordinator")
        old_path = sys.path.copy()
        sys.path.insert(0, handler_dir)

        try:
            # Clear cached modules
            for mod_name in list(sys.modules.keys()):
                if "pipeline_coordinator" in mod_name or mod_name == "handler":
                    del sys.modules[mod_name]

            import importlib
            import handler as coordinator_module
            importlib.reload(coordinator_module)

            # Override module-level clients
            coordinator_module.lambda_client = mock_lambda
            coordinator_module.sfn_client = mock_sfn
            coordinator_module.s3_client = mock_s3
            coordinator_module.dynamodb = mock_dynamodb
            coordinator_module.pipeline_runs_table = mock_pipeline_runs_table
            coordinator_module.alert_state_table = mock_alert_state_table
            coordinator_module.shortage_state_table = mock_shortage_table
            coordinator_module.STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
            coordinator_module.DATA_BUCKET = "healthsignals-data-test"

            return coordinator_module, mock_sfn, mock_lambda, mock_shortage_table
        finally:
            sys.path = old_path


class TestCombinedSignalWhenShortagesExist:
    """Test combined alert generated when shortages exist for the disease."""

    def test_combined_signal_when_shortages_exist(self):
        """Seed DDB shortage-state with antiviral shortage, trigger disease pipeline
        → verify alert_type='combined'."""
        # Influenza maps to antivirals and antibiotics categories
        shortage_items = {
            "antivirals": _shortage_state_items_with_new(),
            "antibiotics": _shortage_state_items_antibiotics(),
        }

        coordinator, mock_sfn, mock_lambda, mock_shortage_table = \
            _setup_pipeline_coordinator(shortage_items)

        # Trigger disease outbreak detection manually
        event = {
            "source": "manual",
            "state_key": "texas",
            "disease_key": "influenza",
            "week": "202403",
        }

        result = coordinator.lambda_handler(event, None)
        body = json.loads(result["body"])

        # Verify SFN was called with combined alert type
        assert mock_sfn.start_execution.called, "Step Functions should be invoked"
        sfn_call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(sfn_call_args[1]["input"])

        assert sfn_input["alert_type"] == "combined", \
            f"Expected alert_type='combined', got '{sfn_input['alert_type']}'"
        assert "shortage_context" in sfn_input, \
            "Combined alert should include shortage_context"


class TestStandardAlertWhenNoShortages:
    """Test standard disease alert when no shortages exist."""

    def test_standard_alert_when_no_shortages(self):
        """Empty shortage-state table → verify alert_type='disease_outbreak'."""
        # No shortage items for any category
        shortage_items = {}

        coordinator, mock_sfn, mock_lambda, mock_shortage_table = \
            _setup_pipeline_coordinator(shortage_items)

        event = {
            "source": "manual",
            "state_key": "texas",
            "disease_key": "influenza",
            "week": "202403",
        }

        result = coordinator.lambda_handler(event, None)

        # Verify SFN was called with standard disease_outbreak type
        assert mock_sfn.start_execution.called, "Step Functions should be invoked"
        sfn_call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(sfn_call_args[1]["input"])

        assert sfn_input["alert_type"] == "disease_outbreak", \
            f"Expected alert_type='disease_outbreak', got '{sfn_input['alert_type']}'"
        assert "shortage_context" not in sfn_input, \
            "Standard alert should NOT include shortage_context"


class TestShortageContextIncludesCorrectProducts:
    """Test that shortage context only includes products from relevant categories."""

    def test_shortage_context_includes_correct_products(self):
        """Verify only products in relevant categories for the disease are included."""
        # Set up: antivirals has a shortage, but respiratory does NOT
        # For influenza, both antivirals and antibiotics are relevant
        shortage_items = {
            "antivirals": _shortage_state_items_with_new(),
            # antibiotics is also relevant to influenza but has no items
            "antibiotics": [],
            # respiratory is relevant but no items
            "respiratory": [],
        }

        coordinator, mock_sfn, mock_lambda, mock_shortage_table = \
            _setup_pipeline_coordinator(shortage_items)

        event = {
            "source": "manual",
            "state_key": "texas",
            "disease_key": "influenza",
            "week": "202403",
        }

        result = coordinator.lambda_handler(event, None)

        # Verify the shortage context was included
        assert mock_sfn.start_execution.called
        sfn_call_args = mock_sfn.start_execution.call_args
        sfn_input = json.loads(sfn_call_args[1]["input"])

        assert sfn_input["alert_type"] == "combined"
        shortage_context = sfn_input["shortage_context"]

        # Should only include the antivirals product (PROD-001)
        affected_products = shortage_context["affected_products"]
        assert len(affected_products) >= 1

        # Verify all included products are from relevant categories
        for product in affected_products:
            assert product["therapeutic_category"] in ["Antivirals", "Antibiotics", "Respiratory Medications"], \
                f"Product from unexpected category: {product['therapeutic_category']}"

        # Verify the specific antiviral product is present
        product_ids = [p["product_id"] for p in affected_products]
        assert "PROD-001" in product_ids, "Expected PROD-001 (Oseltamivir) in shortage context"

        # Verify categories list is populated
        assert "Antivirals" in shortage_context["categories"]
