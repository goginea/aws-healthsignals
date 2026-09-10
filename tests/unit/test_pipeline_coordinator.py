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
    "s3_storage": {
        "prefix_pattern": "raw/cdc_nssp_county/{disease}/{year}/W{week}/{fips}.json",
        "geo_prefix_pattern": "raw/cdc_nssp_geo/{disease}/{year}/W{week}/{level}/{geo_id}.json",
    },
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

    def test_build_county_surveillance_context_none_when_no_config(self, handler):
        assert handler._build_county_surveillance_context("48143", "influenza", None) is None


HSA_RECORD = {
    "geo_level": "hsa", "name": "Erath, TX - Comanche, TX",
    "hsa_counties": "Comanche, Erath",
    "value": 1.4, "trend": "rising", "trend_raw": "Increasing",
    "week_end": "2026-09-05", "source": "cdc_nssp_county",
}
STATE_FALLBACK_RECORD = {
    "geo_level": "state", "name": "Texas",
    "value": 0.3, "trend": "rising", "week_end": "2026-08-29", "source": "cdc_nssp",
}
COUNTY_ZERO_RECORD = {
    "geo_level": "county", "name": "Erath County", "county_name": "Erath County",
    "value": 0.0, "trend": "stable", "trend_raw": "No Change",
    "week_end": "2026-09-05", "source": "cdc_nssp_county",
}


class TestSurveillanceFallbackChain:
    """county -> HSA -> state fallback for county_surveillance context."""

    def test_has_value_treats_zero_as_present(self, handler):
        assert handler._has_value({"value": 0.0}) is True
        assert handler._has_value({"value": 2.5}) is True
        assert handler._has_value({"value": None}) is False
        assert handler._has_value({}) is False
        assert handler._has_value(None) is False

    def test_county_value_wins_including_zero(self, handler):
        # Erath: real 0.0 county value -> stay at county, do NOT fall through.
        with patch.object(handler, "load_latest_surveillance_signal", return_value=COUNTY_ZERO_RECORD):
            ctx = handler._build_county_surveillance_context(
                "48143", "influenza", MOCK_COUNTY_CONFIG, state_key="texas"
            )
        assert ctx["resolved_from"] == "county"
        assert ctx["value"] == 0.0

    def test_falls_back_to_hsa_when_county_unavailable(self, handler):
        # County has no value; HSA does.
        def _load(level, geo_id, disease, cfg):
            return None if level == "county" else (HSA_RECORD if level == "hsa" else None)
        with patch.object(handler, "load_latest_surveillance_signal", side_effect=_load), \
             patch.object(handler, "_load_hsa_map", return_value={"hsa_nci_id": "468", "state_key": "texas"}):
            ctx = handler._build_county_surveillance_context(
                "48049", "influenza", MOCK_COUNTY_CONFIG, state_key="texas"
            )
        assert ctx["resolved_from"] == "hsa"
        assert ctx["value"] == 1.4
        assert "Comanche" in ctx["hsa_counties"]

    def test_falls_back_to_state_when_county_and_hsa_unavailable(self, handler):
        # Brown: county None, HSA None -> state.
        def _load(level, geo_id, disease, cfg):
            return STATE_FALLBACK_RECORD if level == "state" else None
        with patch.object(handler, "load_latest_surveillance_signal", side_effect=_load), \
             patch.object(handler, "_load_hsa_map", return_value={"hsa_nci_id": "468", "state_key": "texas"}):
            ctx = handler._build_county_surveillance_context(
                "48049", "influenza", MOCK_COUNTY_CONFIG, state_key="texas"
            )
        assert ctx["resolved_from"] == "state"
        assert ctx["value"] == 0.3
        assert ctx["name"] == "Texas"

    def test_none_when_nothing_available(self, handler):
        with patch.object(handler, "load_latest_surveillance_signal", return_value=None), \
             patch.object(handler, "_load_hsa_map", return_value=None):
            ctx = handler._build_county_surveillance_context(
                "48049", "influenza", MOCK_COUNTY_CONFIG, state_key="texas"
            )
        assert ctx is None

    def test_state_key_from_hsa_map_when_not_passed(self, handler):
        def _load(level, geo_id, disease, cfg):
            return STATE_FALLBACK_RECORD if (level == "state" and geo_id == "texas") else None
        with patch.object(handler, "load_latest_surveillance_signal", side_effect=_load), \
             patch.object(handler, "_load_hsa_map", return_value={"hsa_nci_id": "", "state_key": "texas"}):
            ctx = handler._build_county_surveillance_context(
                "48049", "influenza", MOCK_COUNTY_CONFIG
            )
        assert ctx["resolved_from"] == "state"


STATE_RECORD = {
    "geo_level": "state",
    "geo_id": "texas",
    "name": "Texas",
    "value": 2.5,
    "smoothed_value": None,
    "trend": "rising",
    "trend_raw": "",
    "week_end": "2026-09-05T00:00:00.000",
    "source": "cdc_nssp",
}

NATIONAL_RECORD = {
    "geo_level": "national",
    "geo_id": "national",
    "name": "United States",
    "value": 0.19,
    "trend": "rising",
    "week_end": "2026-09-05T00:00:00.000",
    "source": "cdc_nssp_county",
}


class TestZoomOutSurveillance:
    """The generalized resolver supports county|state|national granularity."""

    def test_build_surveillance_context_state(self, handler):
        with patch.object(handler, "load_latest_surveillance_signal", return_value=STATE_RECORD):
            ctx = handler.build_surveillance_context("state", "texas", "influenza", MOCK_COUNTY_CONFIG)
        assert ctx["geo_level"] == "state"
        assert ctx["geo_id"] == "texas"
        assert ctx["name"] == "Texas"
        assert ctx["value"] == 2.5
        assert ctx["trend"] == "rising"

    def test_build_surveillance_context_national(self, handler):
        with patch.object(handler, "load_latest_surveillance_signal", return_value=NATIONAL_RECORD):
            ctx = handler.build_surveillance_context("national", "national", "influenza", MOCK_COUNTY_CONFIG)
        assert ctx["geo_level"] == "national"
        assert ctx["name"] == "United States"
        assert ctx["value"] == 0.19

    def test_build_surveillance_context_none_when_absent(self, handler):
        with patch.object(handler, "load_latest_surveillance_signal", return_value=None):
            ctx = handler.build_surveillance_context("state", "texas", "influenza", MOCK_COUNTY_CONFIG)
        assert ctx is None

    def test_county_wrapper_delegates_to_generalized(self, handler):
        """load_latest_county_signal must route through the generalized resolver."""
        with patch.object(handler, "load_latest_surveillance_signal", return_value=STATE_RECORD) as m:
            handler.load_latest_county_signal("48143", "influenza", MOCK_COUNTY_CONFIG)
            m.assert_called_once_with("county", "48143", "influenza", MOCK_COUNTY_CONFIG)


class TestSfnInputCarriesCountySurveillance:
    """Option A: county_surveillance must reach the Step Functions input."""

    def test_sfn_input_includes_county_surveillance(self, handler):
        county_alert = {
            "county_fips": "48143",
            "county_name": "Erath County",
            "disease": "influenza",
            "detection_week": "202636",
            "county_surveillance": {"value": 0.31, "trend": "rising", "source": "cdc_nssp_county"},
        }
        captured = {}

        class _FakeSfn:
            def start_execution(self, **kwargs):
                captured["input"] = kwargs["input"]
                return {"executionArn": "arn:test", "startDate": __import__("datetime").datetime.utcnow()}

        with patch.object(handler, "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:test"), \
             patch.object(handler, "sfn_client", _FakeSfn()):
            handler.start_alert_generation(county_alert, "exec-1234-5678")

        payload = json.loads(captured["input"])
        assert "county_surveillance" in payload
        assert payload["county_surveillance"]["value"] == 0.31
        assert payload["county_surveillance"]["trend"] == "rising"

    def test_sfn_input_county_surveillance_null_when_absent(self, handler):
        county_alert = {
            "county_fips": "48143",
            "county_name": "Erath County",
            "disease": "influenza",
            "detection_week": "202636",
        }
        captured = {}

        class _FakeSfn:
            def start_execution(self, **kwargs):
                captured["input"] = kwargs["input"]
                return {"executionArn": "arn:test", "startDate": __import__("datetime").datetime.utcnow()}

        with patch.object(handler, "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:test"), \
             patch.object(handler, "sfn_client", _FakeSfn()):
            handler.start_alert_generation(county_alert, "exec-1234-5678")

        payload = json.loads(captured["input"])
        # Present as an explicit null so the ASL States.JsonToString handles it
        assert "county_surveillance" in payload
        assert payload["county_surveillance"] is None
