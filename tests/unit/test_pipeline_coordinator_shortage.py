"""Unit tests for Pipeline Coordinator — Drug Shortage Intelligence extensions.

Tests the shortage-related functions added to the pipeline coordinator:
- Routing openFDA shortage data to the shortage handler
- Querying shortage context for disease outbreaks
- Week extraction from shortage S3 keys
- Alert type assignment based on shortage context
"""
import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from boto3.dynamodb.conditions import Key, Attr

from tests.conftest import load_handler


MOCK_SYSTEM = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"},
    "dynamodb_tables": {
        "pipeline_runs": "healthsignals-pipeline-runs-test",
        "alert_state": "healthsignals-alert-state-test",
    },
    "lambda_functions": {
        "leader_detection": "healthsignals-leader-detection",
        "geographic_affinity": "healthsignals-geographic-affinity",
        "timing_estimation": "healthsignals-timing-estimation",
    },
    "step_functions": {"alert_generation_arn": "arn:aws:states:us-east-1:123:stateMachine:test"},
    "orchestration": {"max_counties_per_run": 20, "circuit_breaker_enabled": True},
    "observability": {"log_level": "INFO"},
}

MOCK_THERAPEUTIC_CATEGORIES = {
    "categories": [
        {
            "category_key": "antivirals",
            "display_name": "Antivirals",
            "relevant_diseases": ["influenza", "covid"],
            "priority_level": "HIGH",
        },
        {
            "category_key": "antibiotics",
            "display_name": "Antibiotics",
            "relevant_diseases": ["strep"],
            "priority_level": "MEDIUM",
        },
        {
            "category_key": "respiratory",
            "display_name": "Respiratory Medications",
            "relevant_diseases": ["rsv", "covid"],
            "priority_level": "HIGH",
        },
    ]
}


@pytest.fixture(scope="module")
def handler():
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_lambda_client = MagicMock()
    mock_s3_client = MagicMock()

    def mock_client(service, *args, **kwargs):
        if service == "lambda":
            return mock_lambda_client
        elif service == "s3":
            return mock_s3_client
        elif service == "stepfunctions":
            return MagicMock()
        return MagicMock()

    return load_handler(
        "orchestration/pipeline_coordinator",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.list_active_states": [{"state_key": "texas"}],
            "shared.config_loader.list_active_diseases": [{"disease_key": "influenza"}],
            "shared.config_loader.get_state_config": {"sentinel_metros": {}},
            "shared.config_loader.get_all_sentinel_metros": {},
            "shared.config_loader.get_disease_config": {
                "data_sources": {"delphi": {"signal": "pct_ed_visits_influenza"}}
            },
            "boto3.client": mock_client,
            "boto3.resource": MagicMock(return_value=mock_dynamo),
        },
    )


class TestRoutingShortageData:
    """Tests for S3 event routing to shortage handler."""

    def test_routes_openfda_data_to_shortage_handler(self, handler):
        """S3 key containing 'openfda-shortages' routes to handle_shortage_data()."""
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "healthsignals-data-test"},
                    "object": {"key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json"},
                }
            }]
        }

        with patch.object(handler, "handle_shortage_data", return_value={"changes_detected": {}}) as mock_handle:
            result = handler.lambda_handler(s3_event, None)
            mock_handle.assert_called_once_with(
                "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json"
            )
            assert result["statusCode"] == 200

    def test_routes_delphi_data_to_disease_pipeline(self, handler):
        """S3 key containing 'delphi' routes to existing disease pipeline (not shortage handler)."""
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "healthsignals-data-test"},
                    "object": {"key": "raw/delphi/nssp/pct_ed_visits_influenza/2026/W27/26420.json"},
                }
            }]
        }

        with patch.object(handler, "handle_shortage_data") as mock_shortage, \
             patch.object(handler, "run_detection_pipeline", return_value={"new_alert": False, "detected": False}) as mock_pipeline, \
             patch.object(handler, "record_pipeline_execution"):
            result = handler.lambda_handler(s3_event, None)
            mock_shortage.assert_not_called()
            assert result["statusCode"] == 200


class TestQueryShortageContext:
    """Tests for querying DynamoDB shortage state for disease-relevant medications."""

    def test_query_shortage_context_finds_relevant_shortages(self, handler):
        """disease_key 'influenza' finds antivirals shortages via relevant_diseases mapping."""
        mock_items = [
            {
                "product_id": "prod-001",
                "product_name": "Tamiflu",
                "therapeutic_category": "antivirals",
                "supply_status": "Limited",
                "shortage_status": "NEW",
                "reason_for_shortage": "Manufacturing delay",
                "estimated_resolution_date": "2024-03-01",
            }
        ]

        with patch.object(handler, "_load_therapeutic_categories", return_value=MOCK_THERAPEUTIC_CATEGORIES):
            # Mock the shortage_state_table.query call
            handler.shortage_state_table.query.return_value = {"Items": mock_items}

            result = handler.query_shortage_context("influenza")

            assert result is not None
            assert result["disease_key"] == "influenza"
            assert result["shortage_count"] == 1
            assert len(result["affected_products"]) == 1
            assert result["affected_products"][0]["product_name"] == "Tamiflu"
            assert "Antivirals" in result["categories"]

    def test_query_shortage_context_no_shortages(self, handler):
        """Returns None when no matching shortages exist in DDB."""
        with patch.object(handler, "_load_therapeutic_categories", return_value=MOCK_THERAPEUTIC_CATEGORIES):
            # Mock query returning empty items for all categories
            handler.shortage_state_table.query.return_value = {"Items": []}

            result = handler.query_shortage_context("influenza")

            assert result is None

    def test_query_shortage_context_no_relevant_categories(self, handler):
        """disease_key not in any category's relevant_diseases returns None."""
        with patch.object(handler, "_load_therapeutic_categories", return_value=MOCK_THERAPEUTIC_CATEGORIES):
            # "norovirus" is not in any category's relevant_diseases
            result = handler.query_shortage_context("norovirus")

            assert result is None


class TestExtractWeekFromShortageKey:
    """Tests for parsing week_timestamp from S3 key paths."""

    def test_extract_week_from_shortage_key(self, handler):
        """Parses 'raw/openfda-shortages/2024/W03/shortages_xxx.json' → '2024-W03'."""
        s3_key = "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json"
        result = handler._extract_week_from_shortage_key(s3_key)
        assert result == "2024-W03"

    def test_extract_week_from_shortage_key_double_digit(self, handler):
        """Parses week numbers with double digits correctly."""
        s3_key = "raw/openfda-shortages/2024/W12/shortages_20240320_120000.json"
        result = handler._extract_week_from_shortage_key(s3_key)
        assert result == "2024-W12"

    def test_extract_week_from_shortage_key_fallback(self, handler):
        """Falls back to current ISO week when key cannot be parsed."""
        s3_key = "invalid/key.json"
        result = handler._extract_week_from_shortage_key(s3_key)
        # Should return a valid ISO week format regardless
        assert "-W" in result


class TestAlertTypeAssignment:
    """Tests for alert_type being set based on shortage context."""

    def test_combined_alert_type_when_shortages_exist(self, handler):
        """county_alert gets alert_type='combined' when shortage_context is populated."""
        shortage_context = {
            "affected_products": [{"product_name": "Tamiflu", "therapeutic_category": "Antivirals"}],
            "categories": ["Antivirals"],
            "disease_key": "influenza",
            "shortage_count": 1,
        }

        # Mock the full run_detection_pipeline to test alert_type assignment
        with patch.object(handler, "load_latest_metro_signals", return_value={"26420": {"value": 5.2, "trend": "rising"}}), \
             patch.object(handler, "invoke_lambda_sync") as mock_invoke, \
             patch.object(handler, "query_shortage_context", return_value=shortage_context):

            # Mock leader detection response
            mock_invoke.side_effect = [
                # leader_detection result
                {
                    "detected": True,
                    "new_alert": True,
                    "leader": {"msa_code": "26420", "metro_name": "Houston", "value": 5.2},
                },
                # geographic_affinity result
                {
                    "affected_counties": [
                        {"county_fips": "48143", "county_name": "Erath County", "affinity_weight": 0.75}
                    ]
                },
                # timing_estimation result
                {
                    "estimated_lag_weeks": 3,
                    "severity_multiplier": 1.8,
                    "confidence": 0.72,
                    "seasons_calibrated": 4,
                    "warning_window_weeks": 3,
                    "cdc_activity_level": "high",
                },
            ]

            result = handler.run_detection_pipeline(
                state_key="texas",
                disease_key="influenza",
                week="202603",
                execution_id="test-exec-001",
            )

            assert result["new_alert"] is True
            counties = result["counties_alerted"]
            assert len(counties) == 1
            assert counties[0]["alert_type"] == "combined"
            assert counties[0]["shortage_context"] == shortage_context

    def test_disease_outbreak_type_when_no_shortages(self, handler):
        """county_alert gets alert_type='disease_outbreak' when no shortages found."""
        with patch.object(handler, "load_latest_metro_signals", return_value={"26420": {"value": 5.2, "trend": "rising"}}), \
             patch.object(handler, "invoke_lambda_sync") as mock_invoke, \
             patch.object(handler, "query_shortage_context", return_value=None):

            mock_invoke.side_effect = [
                # leader_detection result
                {
                    "detected": True,
                    "new_alert": True,
                    "leader": {"msa_code": "26420", "metro_name": "Houston", "value": 5.2},
                },
                # geographic_affinity result
                {
                    "affected_counties": [
                        {"county_fips": "48143", "county_name": "Erath County", "affinity_weight": 0.75}
                    ]
                },
                # timing_estimation result
                {
                    "estimated_lag_weeks": 4,
                    "severity_multiplier": 1.5,
                    "confidence": 0.6,
                    "seasons_calibrated": 3,
                    "warning_window_weeks": 4,
                    "cdc_activity_level": "moderate",
                },
            ]

            result = handler.run_detection_pipeline(
                state_key="texas",
                disease_key="influenza",
                week="202603",
                execution_id="test-exec-002",
            )

            assert result["new_alert"] is True
            counties = result["counties_alerted"]
            assert len(counties) == 1
            assert counties[0]["alert_type"] == "disease_outbreak"
            assert "shortage_context" not in counties[0]
