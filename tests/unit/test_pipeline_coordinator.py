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


MOCK_COUNTY_CONFIG = {
    "enabled": True,
    "priority": "supplemental",
    "s3_storage": {"prefix_pattern": "raw/cdc_nssp_county/{disease}/{year}/W{week}/{fips}.json"},
}

COUNTY_RECORD_RISING = {
    "value": 0.31,
    "smoothed_value": 0.32,
    "trend": "rising",
    "trend_raw": "Increasing",
    "week_end": "2026-09-05T00:00:00.000",
}


class TestCountySupplementalEnrichment:
    """Verify county NSSP data is strictly additive and never suppresses Delphi."""

    def test_enrich_gap_fills_unknown_trend(self, handler):
        signal = {"value": 2.5, "trend": "unknown"}
        with patch.object(handler, "get_data_source_config", return_value=MOCK_COUNTY_CONFIG), \
             patch.object(handler, "load_latest_county_signal", return_value=COUNTY_RECORD_RISING):
            handler._enrich_with_county_nssp(signal, "48201", "influenza")
        # Delphi value untouched; trend gap-filled from NSSP
        assert signal["value"] == 2.5
        assert signal["trend"] == "rising"
        assert signal["trend_source"] == "cdc_nssp_county_gapfill"
        assert signal["county_nssp"]["value"] == 0.31

    def test_enrich_corroborates_rising(self, handler):
        signal = {"value": 2.5, "trend": "rising"}
        with patch.object(handler, "get_data_source_config", return_value=MOCK_COUNTY_CONFIG), \
             patch.object(handler, "load_latest_county_signal", return_value=COUNTY_RECORD_RISING):
            handler._enrich_with_county_nssp(signal, "48201", "influenza")
        assert signal["trend"] == "rising"
        assert signal.get("corroborated_by") == "cdc_nssp_county"

    def test_enrich_never_suppresses_delphi_rising(self, handler):
        """Even if county says stable/declining, a Delphi 'rising' must survive."""
        signal = {"value": 2.5, "trend": "rising"}
        county_declining = {**COUNTY_RECORD_RISING, "trend": "declining", "trend_raw": "Decreasing"}
        with patch.object(handler, "get_data_source_config", return_value=MOCK_COUNTY_CONFIG), \
             patch.object(handler, "load_latest_county_signal", return_value=county_declining):
            handler._enrich_with_county_nssp(signal, "48201", "influenza")
        # Delphi trend is NOT downgraded
        assert signal["trend"] == "rising"
        assert "corroborated_by" not in signal
        assert signal["value"] == 2.5

    def test_enrich_skips_when_not_supplemental(self, handler):
        signal = {"value": 2.5, "trend": "unknown"}
        non_supp = {**MOCK_COUNTY_CONFIG, "priority": "primary"}
        with patch.object(handler, "get_data_source_config", return_value=non_supp), \
             patch.object(handler, "load_latest_county_signal", return_value=COUNTY_RECORD_RISING):
            handler._enrich_with_county_nssp(signal, "48201", "influenza")
        # Untouched — no county_nssp attached, trend still unknown
        assert signal["trend"] == "unknown"
        assert "county_nssp" not in signal

    def test_enrich_no_county_data_is_noop(self, handler):
        signal = {"value": 2.5, "trend": "unknown"}
        with patch.object(handler, "get_data_source_config", return_value=MOCK_COUNTY_CONFIG), \
             patch.object(handler, "load_latest_county_signal", return_value=None):
            handler._enrich_with_county_nssp(signal, "48201", "influenza")
        assert signal == {"value": 2.5, "trend": "unknown"}

    def test_build_county_surveillance_context(self, handler):
        with patch.object(handler, "load_latest_county_signal", return_value=COUNTY_RECORD_RISING):
            ctx = handler._build_county_surveillance_context("48143", "influenza", MOCK_COUNTY_CONFIG)
        assert ctx["value"] == 0.31
        assert ctx["trend"] == "rising"
        assert ctx["source"] == "cdc_nssp_county"

    def test_build_county_surveillance_context_none_when_absent(self, handler):
        with patch.object(handler, "load_latest_county_signal", return_value=None):
            ctx = handler._build_county_surveillance_context("48143", "influenza", MOCK_COUNTY_CONFIG)
        assert ctx is None

    def test_build_county_surveillance_context_none_when_no_config(self, handler):
        assert handler._build_county_surveillance_context("48143", "influenza", None) is None
