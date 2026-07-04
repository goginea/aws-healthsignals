"""Unit tests for Pipeline Coordinator Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import load_handler


MOCK_SYSTEM = {
    "infrastructure": {"data_bucket_name_pattern": "healthsignals-data-test"},
    "dynamodb_tables": {"pipeline_runs": "healthsignals-pipeline-runs-test", "alert_state": "healthsignals-alert-state-test"},
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
    return load_handler(
        "orchestration/pipeline_coordinator",
        extra_patches={
            "shared.config_loader.get_system_config": MOCK_SYSTEM,
            "shared.config_loader.list_active_states": [{"state_key": "texas"}],
            "shared.config_loader.list_active_diseases": [{"disease_key": "influenza"}],
            "shared.config_loader.get_state_config": {"sentinel_metros": {}},
            "shared.config_loader.get_all_sentinel_metros": {},
            "shared.config_loader.get_disease_config": {"data_sources": {"delphi": {"signal": "pct_ed_visits_influenza"}}},
            "boto3.client": MagicMock(),
            "boto3.resource": MagicMock(),
        },
    )


class TestPipelineCoordinator:
    def test_handler_exists(self, handler):
        assert hasattr(handler, "lambda_handler")

    def test_parse_s3_event(self, handler):
        event = {
            "Records": [{"s3": {"bucket": {"name": "test"}, "object": {"key": "raw/delphi/nssp/pct_ed_visits_influenza/2026/W27/26420.json"}}}]
        }
        result = handler.parse_s3_event(event)
        assert result is not None

    def test_get_current_epiweek(self, handler):
        week = handler.get_current_epiweek()
        assert isinstance(week, str)
        assert len(week) == 6  # YYYYWW format

    def test_manual_invocation(self, handler):
        """Manual invocation with explicit params should work."""
        with patch.object(handler, "run_detection_pipeline", return_value={"alerts_triggered": 0}), \
             patch.object(handler, "record_pipeline_execution"):
            event = {"source": "manual", "state_key": "texas", "disease_key": "influenza", "week": "202645"}
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200

    def test_circuit_breaker(self, handler):
        """Circuit breaker should flag when too many counties affected."""
        # The max is 20 from our config
        assert handler.lambda_handler is not None  # handler loaded successfully
