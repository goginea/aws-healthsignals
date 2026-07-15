"""Unit tests for Pipeline Coordinator — EventBridge event emission.

Tests the event-driven plugin architecture:
- EventBridge event emitted on disease threshold detection
- Alert type is always 'disease_outbreak' (combined enrichment handled by plugin)
- EventBridge failure does not block the pipeline
"""
import json
import pytest
from unittest.mock import patch, MagicMock

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


@pytest.fixture(scope="module")
def handler():
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_lambda_client = MagicMock()
    mock_s3_client = MagicMock()
    mock_events_client = MagicMock()

    def mock_client(service, *args, **kwargs):
        if service == "lambda":
            return mock_lambda_client
        elif service == "s3":
            return mock_s3_client
        elif service == "stepfunctions":
            return MagicMock()
        elif service == "events":
            return mock_events_client
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


class TestEventBridgeEmission:
    """Tests for EventBridge disease threshold event emission."""

    def test_emits_event_on_disease_detection(self, handler):
        """Verify EventBridge event is emitted after disease threshold is crossed."""
        county_alerts = [
            {
                "county_fips": "48143",
                "county_name": "Erath County",
                "disease": "influenza",
                "leader_metro_name": "Houston",
                "leader_value": 5.2,
                "detection_week": "202645",
                "alert_type": "disease_outbreak",
            }
        ]
        leader = {"msa_code": "26420", "metro_name": "Houston", "value": 5.2}

        handler.events_client = MagicMock()

        handler.emit_disease_threshold_event(
            disease_key="influenza",
            state_key="texas",
            week="202645",
            leader=leader,
            county_alerts=county_alerts,
        )

        handler.events_client.put_events.assert_called_once()
        call_args = handler.events_client.put_events.call_args
        entries = call_args[1]["Entries"] if "Entries" in call_args[1] else call_args[0][0]

        # Verify event structure
        entry = entries[0]
        assert entry["Source"] == "healthsignals.pipeline_coordinator"
        assert entry["DetailType"] == "healthsignals.disease.threshold_crossed"

        detail = json.loads(entry["Detail"])
        assert detail["disease_key"] == "influenza"
        assert detail["state_key"] == "texas"
        assert detail["week"] == "202645"
        assert detail["leader"] == leader
        assert len(detail["county_alerts"]) == 1

    def test_eventbridge_failure_does_not_block_pipeline(self, handler):
        """EventBridge emit failure logs error but does not raise."""
        handler.events_client = MagicMock()
        handler.events_client.put_events.side_effect = Exception("EventBridge unavailable")

        # Should not raise
        handler.emit_disease_threshold_event(
            disease_key="influenza",
            state_key="texas",
            week="202645",
            leader={"msa_code": "26420", "value": 5.2},
            county_alerts=[{"county_fips": "48143"}],
        )
        # If we get here without exception, test passes

    def test_event_contains_correct_bus_name(self, handler):
        """Event is sent to the configured EVENT_BUS_NAME."""
        handler.events_client = MagicMock()
        handler.EVENT_BUS_NAME = "custom-bus"

        handler.emit_disease_threshold_event(
            disease_key="covid",
            state_key="california",
            week="202610",
            leader={"msa_code": "31080", "value": 8.1},
            county_alerts=[],
        )

        call_args = handler.events_client.put_events.call_args
        entries = call_args[1]["Entries"] if "Entries" in call_args[1] else call_args[0][0]
        assert entries[0]["EventBusName"] == "custom-bus"

        # Reset
        handler.EVENT_BUS_NAME = "default"


class TestAlertTypeAssignment:
    """Tests that alert_type is always 'disease_outbreak' (no more inline shortage enrichment)."""

    def test_alert_type_always_disease_outbreak(self, handler):
        """county_alert always gets alert_type='disease_outbreak' — enrichment is external."""
        with patch.object(handler, "load_latest_metro_signals", return_value={"26420": {"value": 5.2, "trend": "rising"}}), \
             patch.object(handler, "invoke_lambda_sync") as mock_invoke:

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
                execution_id="test-exec-001",
            )

            assert result["new_alert"] is True
            counties = result["counties_alerted"]
            assert len(counties) == 1
            assert counties[0]["alert_type"] == "disease_outbreak"
            assert "shortage_context" not in counties[0]

    def test_no_shortage_functions_exist(self, handler):
        """Verify shortage functions have been removed from the coordinator."""
        assert not hasattr(handler, "handle_shortage_data")
        assert not hasattr(handler, "query_shortage_context")
        assert not hasattr(handler, "_extract_week_from_shortage_key")
        assert not hasattr(handler, "_load_therapeutic_categories")


class TestNoShortageRouting:
    """Tests that the coordinator no longer routes openFDA data."""

    def test_openfda_s3_key_not_routed(self, handler):
        """S3 key with 'openfda-shortages' is no longer handled by coordinator."""
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "healthsignals-data-test"},
                    "object": {"key": "raw/openfda-shortages/2024/W03/shortages_20240115.json"},
                }
            }]
        }

        # The coordinator should try to parse this as a Delphi event and
        # proceed to run_detection_pipeline (which will find no metro signals)
        with patch.object(handler, "run_detection_pipeline", return_value={"new_alert": False, "detected": False}) as mock_pipeline, \
             patch.object(handler, "record_pipeline_execution"):
            result = handler.lambda_handler(s3_event, None)
            # Should not crash — it just won't find Delphi data for this key
            assert result["statusCode"] in (200, 207)

    def test_delphi_data_still_routes_correctly(self, handler):
        """Delphi S3 data continues to route to disease pipeline."""
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "healthsignals-data-test"},
                    "object": {"key": "raw/delphi/nssp/pct_ed_visits_influenza/2026/W27/26420.json"},
                }
            }]
        }

        with patch.object(handler, "run_detection_pipeline", return_value={"new_alert": False, "detected": False}) as mock_pipeline, \
             patch.object(handler, "record_pipeline_execution"):
            result = handler.lambda_handler(s3_event, None)
            assert result["statusCode"] == 200
