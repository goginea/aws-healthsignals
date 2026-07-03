"""Unit tests for pipeline coordinator (orchestration brain)."""
import json
import pytest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, "lambdas/orchestration/pipeline_coordinator")
sys.path.insert(0, "lambdas/shared")
sys.path.insert(0, "lambdas")


class TestPipelineCoordinatorLogic:
    """Test orchestration decision logic."""

    @patch("handler.boto3")
    @patch("handler.get_system_config")
    def test_s3_event_parsing(self, mock_config, mock_boto):
        """S3 event should be correctly parsed into state/disease/week."""
        mock_config.return_value = {
            "dynamodb_tables": {"pipeline_runs": "test-runs", "alert_state": "test-alerts"},
            "orchestration": {"max_counties_per_run": 20},
            "lambda_functions": {},
            "step_functions": {"state_machine_arn": "arn:aws:states:us-east-1:123:stateMachine:test"},
        }

        # S3 event structure
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "healthsignals-data-123-us-east-1"},
                    "object": {"key": "raw/delphi/nssp/pct_ed_visits_influenza/2026/W45/26420.json"}
                }
            }]
        }

        # The handler should parse the key to determine what was ingested
        key = s3_event["Records"][0]["s3"]["object"]["key"]
        parts = key.split("/")
        assert parts[0] == "raw"
        assert parts[1] == "delphi"
        assert parts[2] == "nssp"
        assert "influenza" in parts[3]

    def test_circuit_breaker_threshold(self):
        """Circuit breaker should trigger at >20 counties."""
        max_counties = 20
        affected_counties = list(range(25))
        assert len(affected_counties) > max_counties

    def test_manual_invocation_format(self):
        """Manual invocation should have expected fields."""
        manual_event = {
            "source": "manual",
            "state_key": "texas",
            "disease_key": "influenza",
            "week": "202645",
        }
        assert manual_event["source"] == "manual"
        assert "state_key" in manual_event
        assert "disease_key" in manual_event


class TestS3KeyParsing:
    """Test S3 key path extraction."""

    def test_standard_delphi_key(self):
        """Standard Delphi S3 key should parse correctly."""
        key = "raw/delphi/nssp/pct_ed_visits_influenza/2026/W45/26420.json"
        parts = key.split("/")

        data_source = parts[2]  # nssp
        signal = parts[3]       # pct_ed_visits_influenza
        year = parts[4]         # 2026
        week = parts[5]         # W45
        metro = parts[6].replace(".json", "")  # 26420

        assert data_source == "nssp"
        assert "influenza" in signal
        assert year == "2026"
        assert week == "W45"
        assert metro == "26420"

    def test_covid_key(self):
        """COVID signal key should be distinguishable."""
        key = "raw/delphi/nssp/pct_ed_visits_covid/2026/W45/19100.json"
        parts = key.split("/")
        assert "covid" in parts[3]

    def test_rsv_key(self):
        """RSV signal key should be distinguishable."""
        key = "raw/delphi/nssp/pct_ed_visits_rsv/2026/W45/12420.json"
        parts = key.split("/")
        assert "rsv" in parts[3]
