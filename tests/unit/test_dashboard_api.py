"""Unit tests for the admin dashboard read API (lambdas/dashboard/api/handler.py).

Covers routing, CloudFormation status simplification, pipeline/run-history
assembly, enabled-plugin derivation, and per-pipeline run-output parsing
(core / shortage / outbreak / failed).
"""
import json
import os
import sys
import types
import importlib.util
from datetime import datetime
import pytest
from unittest.mock import MagicMock, patch

LAMBDAS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")


@pytest.fixture()
def handler():
    """Load the dashboard api handler with mocked boto3 clients."""
    module_path = os.path.join(LAMBDAS_ROOT, "dashboard", "api", "handler.py")
    spec = importlib.util.spec_from_file_location("dashboard_api", module_path)
    module = types.ModuleType("dashboard_api")
    module.__spec__ = spec
    module.__file__ = module_path
    with patch("boto3.client", MagicMock()), \
         patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://d.cloudfront.net"}, clear=False):
        spec.loader.exec_module(module)
    return module


def _bedrock(text):
    return {"Body": {"content": [{"text": text}]}}


# --- routing ---------------------------------------------------------------


class TestRouting:
    def test_unknown_route_404(self, handler):
        resp = handler.lambda_handler({"httpMethod": "GET", "resource": "/nope"}, None)
        assert resp["statusCode"] == 404

    def test_runs_requires_arn(self, handler):
        resp = handler.lambda_handler(
            {"httpMethod": "GET", "resource": "/runs", "queryStringParameters": None}, None
        )
        assert resp["statusCode"] == 400

    def test_options_preflight_ok(self, handler):
        resp = handler.lambda_handler({"httpMethod": "OPTIONS", "resource": "/status"}, None)
        assert resp["statusCode"] == 200
        assert resp["headers"]["Access-Control-Allow-Origin"] == "https://d.cloudfront.net"


# --- status ----------------------------------------------------------------


class TestStatus:
    def test_simplify_status(self, handler):
        assert handler._simplify_status("CREATE_COMPLETE") == "deployed"
        assert handler._simplify_status("UPDATE_COMPLETE") == "deployed"
        assert handler._simplify_status("UPDATE_ROLLBACK_COMPLETE") == "failed"
        assert handler._simplify_status("CREATE_FAILED") == "failed"
        assert handler._simplify_status("UPDATE_IN_PROGRESS") == "in_progress"

    def test_status_marks_missing_plugin_not_deployed(self, handler):
        # describe_stacks raises "does not exist" for plugin stacks that aren't deployed.
        class _CE(Exception):
            pass
        handler.cfn.exceptions.ClientError = _CE

        def _describe(StackName):
            if StackName in handler.CORE_STACKS:
                return {"Stacks": [{
                    "StackStatus": "UPDATE_COMPLETE",
                    "CreationTime": datetime(2026, 1, 1),
                    "DriftInformation": {"StackDriftStatus": "IN_SYNC"},
                }]}
            raise _CE(f"Stack with id {StackName} does not exist")

        handler.cfn.describe_stacks.side_effect = _describe
        out = handler.get_status()
        assert all(c["status"] == "deployed" for c in out["core"])
        # every plugin stack raised "does not exist" -> not deployed / disabled
        assert all(p["status"] == "not_deployed" and p["enabled"] is False for p in out["plugins"])


# --- pipelines -------------------------------------------------------------


class TestPipelines:
    def test_only_enabled_plugins_listed(self, handler):
        class _CE(Exception):
            pass
        handler.cfn.exceptions.ClientError = _CE

        # Only the drug-shortage plugin stack exists.
        def _describe(StackName):
            if StackName == "HealthSignals-DrugShortage":
                return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "CreationTime": datetime(2026, 1, 1)}]}
            raise _CE("does not exist")
        handler.cfn.describe_stacks.side_effect = _describe

        with patch.object(handler, "_recent_runs", return_value=[{"name": "r1"}]):
            out = handler.get_pipelines()

        keys = [p["key"] for p in out["pipelines"]]
        assert keys[0] == "core"
        assert "drug_shortage" in keys
        assert "cdc_outbreak" not in keys       # stack absent
        assert "forecast_providers" not in keys  # stack absent
        # core exposes config-driven data sources + Bedrock model (not a step
        # diagram). With no CONFIG_BUCKET set in the test env, these resolve to
        # empty structures rather than reading S3.
        core = next(p for p in out["pipelines"] if p["key"] == "core")
        assert "steps" not in core
        assert core["data_sources"] == []
        assert core["bedrock"] == {
            "routine_model_id": "",
            "high_severity_model_id": "",
            "severity_threshold_for_upgrade": [],
        }

    def test_pipeline_config_from_s3(self, handler):
        """data_sources + bedrock come from S3 config when CONFIG_BUCKET is set."""
        handler.CONFIG_BUCKET = "healthsignals-data-123-us-east-1"
        handler.CONFIG_PREFIX = "config/"

        # Only the core state machine matters here; no plugin stacks deployed.
        class _CE(Exception):
            pass
        handler.cfn.exceptions.ClientError = _CE
        handler.cfn.describe_stacks.side_effect = _CE("Stack does not exist")

        # S3 list of data_sources/ -> two files.
        paginator = MagicMock()
        paginator.paginate.return_value = [{
            "Contents": [
                {"Key": "config/data_sources/delphi.json"},
                {"Key": "config/data_sources/cdc_wastewater.json"},
                {"Key": "config/data_sources/_notes.txt"},  # non-json ignored
            ]
        }]
        handler.s3.get_paginator.return_value = paginator

        bodies = {
            "config/data_sources/delphi.json": {
                "source_name": "delphi", "display_name": "CMU Delphi Epidata API",
                "enabled": True, "priority": "primary",
            },
            "config/data_sources/cdc_wastewater.json": {
                "source_name": "cdc_wastewater", "display_name": "CDC NWSS Wastewater",
                "enabled": True, "priority": "supplemental",
            },
            "config/system.json": {
                "bedrock": {
                    "routine_model_id": "us.anthropic.claude-sonnet-4-5-x",
                    "high_severity_model_id": "us.anthropic.claude-sonnet-5",
                    "severity_threshold_for_upgrade": ["HIGH", "CRITICAL"],
                }
            },
        }

        def _get_object(Bucket, Key):
            body = MagicMock()
            body.read.return_value = json.dumps(bodies[Key]).encode()
            return {"Body": body}
        handler.s3.get_object.side_effect = _get_object

        try:
            with patch.object(handler, "_recent_runs", return_value=[]):
                out = handler.get_pipelines()
        finally:
            handler.CONFIG_BUCKET = ""

        core = next(p for p in out["pipelines"] if p["key"] == "core")
        names = [s["source_name"] for s in core["data_sources"]]
        # core maps to delphi + cdc_wastewater (the two we stubbed); order follows
        # PIPELINE_DATA_SOURCES, and only present sources are included.
        assert names == ["delphi", "cdc_wastewater"]
        assert core["data_sources"][0]["priority"] == "primary"
        assert core["bedrock"]["routine_model_id"] == "us.anthropic.claude-sonnet-4-5-x"
        assert core["bedrock"]["high_severity_model_id"] == "us.anthropic.claude-sonnet-5"
        assert core["bedrock"]["severity_threshold_for_upgrade"] == ["HIGH", "CRITICAL"]

    def test_recent_runs_maps_executions(self, handler):
        handler.sfn.get_paginator.return_value.paginate.return_value = [
            {"stateMachines": [{"name": "healthsignals-alert-generation", "stateMachineArn": "arn:sm"}]}
        ]
        handler.sfn.list_executions.return_value = {"executions": [
            {"executionArn": "arn:ex1", "name": "48049-covid-202636-abcd",
             "status": "SUCCEEDED", "startDate": datetime(2026, 9, 9), "stopDate": datetime(2026, 9, 9)},
        ]}
        runs = handler._recent_runs("healthsignals-alert-generation")
        assert len(runs) == 1
        assert runs[0]["status"] == "SUCCEEDED"
        assert runs[0]["execution_arn"] == "arn:ex1"


# --- run detail parsing ----------------------------------------------------


class TestRunDetail:
    def _desc(self, output, status="SUCCEEDED"):
        d = {"name": "run", "status": status,
             "startDate": datetime(2026, 9, 9), "stopDate": datetime(2026, 9, 9)}
        if output is not None:
            d["output"] = json.dumps(output)
        return d

    def test_core_run_parsed(self, handler):
        output = {
            "situation_brief_result": _bedrock("CORE situation brief text"),
            "severity_result": _bedrock(json.dumps({"severity": "HIGH", "confidence": 0.8})),
            "communication_result": _bedrock("Dear colleagues, email body"),
            "delivery_result": {"Payload": {"total_dispatched": 1, "success": True}},
        }
        handler.sfn.describe_execution.return_value = self._desc(output)
        r = handler.get_run_detail("arn:ex")
        assert "CORE situation brief" in r["brief"]
        assert r["classification"]["severity"] == "HIGH"
        assert "email body" in r["email"]
        assert r["delivery"]["total_dispatched"] == 1

    def test_shortage_run_parsed(self, handler):
        output = {"shortage_brief_result": _bedrock("DRUG SHORTAGE BRIEF"),
                  "delivery_result": {"total_dispatched": 1}}
        handler.sfn.describe_execution.return_value = self._desc(output)
        r = handler.get_run_detail("arn:ex")
        assert "DRUG SHORTAGE BRIEF" in r["brief"]
        # no communication_result -> email falls back to the brief
        assert "DRUG SHORTAGE BRIEF" in r["email"]

    def test_outbreak_run_parsed(self, handler):
        output = {"brief_result": _bedrock("OUTBREAK BRIEF"),
                  "severity_result": _bedrock(json.dumps({"severity": "MODERATE"}))}
        handler.sfn.describe_execution.return_value = self._desc(output)
        r = handler.get_run_detail("arn:ex")
        assert "OUTBREAK BRIEF" in r["brief"]
        assert r["classification"]["severity"] == "MODERATE"

    def test_failed_run_no_output(self, handler):
        handler.sfn.describe_execution.return_value = self._desc(None, status="FAILED")
        r = handler.get_run_detail("arn:ex")
        assert r["status"] == "FAILED"
        assert r["brief"] == ""
        assert "failed" in r["error"].lower()

    def test_bedrock_text_handles_json_string_body(self, handler):
        node = {"Body": json.dumps({"content": [{"text": "stringified"}]})}
        assert handler._bedrock_text(node) == "stringified"

    def test_severity_parsed_from_markdown_fence(self, handler):
        fenced = '```json\n{"severity": "MODERATE", "confidence": 0.75}\n```'
        out = {"severity_result": _bedrock(fenced)}
        handler.sfn.describe_execution.return_value = self._desc(out)
        r = handler.get_run_detail("arn:ex")
        assert r["classification"]["severity"] == "MODERATE"
        assert r["classification"]["confidence"] == 0.75

    def test_severity_parsed_plain_and_embedded(self, handler):
        assert handler._parse_severity('{"severity":"HIGH"}')["severity"] == "HIGH"
        assert handler._parse_severity('prefix {"severity":"LOW"} suffix')["severity"] == "LOW"
        assert handler._parse_severity("not json at all")["raw"] == "not json at all"
