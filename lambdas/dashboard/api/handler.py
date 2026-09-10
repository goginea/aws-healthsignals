"""HealthSignals Admin Dashboard — read-only status API.

Backs the admin dashboard (static CloudFront site + Cognito login). All routes
are read-only and return JSON. Auth is enforced at API Gateway by a Cognito
User Pool authorizer, so this handler does not re-check identity.

Routes (API Gateway REST, proxy integration):
    GET  /status            -> CloudFormation status for all HealthSignals stacks
    POST /drift/{stack}      -> start + report on-demand drift detection for one stack
    GET  /pipelines         -> core + enabled-plugin pipelines, each with last 5 runs
    GET  /runs?arn=<execArn> -> one execution's brief / classification / email / delivery

Environment:
    ALLOWED_ORIGIN   CloudFront origin for CORS (e.g. https://dxxxx.cloudfront.net)
    PIPELINE_RUNS_TABLE  (optional) DynamoDB table for coordinator run counts
    LOG_LEVEL
"""
import json
import os
import logging
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cfn = boto3.client("cloudformation")
sfn = boto3.client("stepfunctions")

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

# Core stacks are always present; plugin stacks may or may not be deployed.
CORE_STACKS = [
    "HealthSignals-Ingestion",
    "HealthSignals-Prediction",
    "HealthSignals-Generation",
    "HealthSignals-Orchestration",
    "HealthSignals-Delivery",
    "HealthSignals-Subscription",
    "HealthSignals-Monitoring",
]
# plugin_key -> (stack name, state machine name)
PLUGIN_STACKS = {
    "drug_shortage": ("HealthSignals-DrugShortage", "healthsignals-shortage-alert-generation"),
    "cdc_outbreak": ("HealthSignals-CDCOutbreaks", "healthsignals-outbreak-alert-generation"),
    "forecast_providers": ("HealthSignals-ForecastProviders", None),
}
CORE_STATE_MACHINE = "healthsignals-alert-generation"

# Map raw CloudFormation StackStatus -> a simple dashboard state.
_FAILED = ("ROLLBACK", "FAILED", "DELETE")
_IN_PROGRESS = ("IN_PROGRESS",)


def lambda_handler(event: dict, context: Any) -> dict:
    """API Gateway proxy entrypoint — dispatch by method + path."""
    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("resource") or event.get("path") or ""
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}

    try:
        if method == "OPTIONS":
            return _resp(200, {"ok": True})
        if method == "GET" and path.rstrip("/").endswith("/status"):
            return _resp(200, get_status())
        if method == "POST" and "/drift" in path:
            return _resp(200, detect_drift(params.get("stack", "")))
        if method == "GET" and path.rstrip("/").endswith("/pipelines"):
            return _resp(200, get_pipelines())
        if method == "GET" and path.rstrip("/").endswith("/runs"):
            arn = (query or {}).get("arn")
            if not arn:
                return _resp(400, {"error": "missing 'arn' query parameter"})
            return _resp(200, get_run_detail(arn))
        return _resp(404, {"error": f"no route for {method} {path}"})
    except Exception as e:
        logger.exception("dashboard api error")
        return _resp(500, {"error": str(e)})


# --- GET /status ------------------------------------------------------------


def _simplify_status(cfn_status: str) -> str:
    if any(t in cfn_status for t in _FAILED):
        return "failed"
    if any(t in cfn_status for t in _IN_PROGRESS):
        return "in_progress"
    if cfn_status.endswith("COMPLETE"):
        return "deployed"
    return "unknown"


def _describe_stack(stack_name: str) -> Optional[dict]:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        stacks = resp.get("Stacks", [])
        if not stacks:
            return None
        s = stacks[0]
        raw = s.get("StackStatus", "")
        updated = s.get("LastUpdatedTime") or s.get("CreationTime")
        return {
            "stack": stack_name,
            "status": _simplify_status(raw),
            "raw_status": raw,
            "updated_at": updated.isoformat() if updated else None,
            "drift_status": s.get("DriftInformation", {}).get("StackDriftStatus"),
        }
    except cfn.exceptions.ClientError as e:
        # Stack doesn't exist (plugin disabled) or access issue.
        if "does not exist" in str(e):
            return None
        raise


def get_status() -> dict:
    """CloudFormation status for core + plugin stacks. Cheap (Describe only)."""
    core = []
    for name in CORE_STACKS:
        info = _describe_stack(name)
        core.append(info or {"stack": name, "status": "not_deployed"})

    plugins = []
    for key, (stack_name, _sm) in PLUGIN_STACKS.items():
        info = _describe_stack(stack_name)
        plugins.append({
            "plugin": key,
            "enabled": info is not None,
            **(info or {"stack": stack_name, "status": "not_deployed"}),
        })

    return {"core": core, "plugins": plugins}


# --- POST /drift/{stack} -----------------------------------------------------


def detect_drift(stack_name: str) -> dict:
    """Start drift detection for one stack and return current resource drift.

    Drift detection is asynchronous and can take tens of seconds. We kick it
    off and report the detection status; the frontend polls or reads the last
    known result. To keep the request bounded we do NOT block on completion.
    """
    if not stack_name or not (stack_name in CORE_STACKS or
                              any(stack_name == s for s, _ in PLUGIN_STACKS.values())):
        return {"error": f"unknown stack '{stack_name}'"}
    try:
        det = cfn.detect_stack_drift(StackName=stack_name)
        detection_id = det["StackDriftDetectionId"]
        status = cfn.describe_stack_drift_detection_status(
            StackDriftDetectionId=detection_id
        )
        return {
            "stack": stack_name,
            "detection_id": detection_id,
            "detection_status": status.get("DetectionStatus"),
            "drift_status": status.get("StackDriftStatus"),
            "drifted_resources": status.get("DriftedStackResourceCount"),
            "note": "Drift detection runs asynchronously; re-request to refresh.",
        }
    except Exception as e:
        return {"stack": stack_name, "error": str(e)}


# --- GET /pipelines ----------------------------------------------------------


def _state_machine_arn(name: str) -> Optional[str]:
    """Resolve a state-machine name to its ARN via ListStateMachines."""
    paginator = sfn.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for sm in page.get("stateMachines", []):
            if sm.get("name") == name:
                return sm.get("stateMachineArn")
    return None


def _recent_runs(state_machine_name: str, limit: int = 5) -> list:
    arn = _state_machine_arn(state_machine_name)
    if not arn:
        return []
    resp = sfn.list_executions(stateMachineArn=arn, maxResults=limit)
    runs = []
    for ex in resp.get("executions", []):
        runs.append({
            "execution_arn": ex.get("executionArn"),
            "name": ex.get("name"),
            "status": ex.get("status"),
            "started_at": ex["startDate"].isoformat() if ex.get("startDate") else None,
            "stopped_at": ex["stopDate"].isoformat() if ex.get("stopDate") else None,
        })
    return runs


def get_pipelines() -> dict:
    """Core + enabled-plugin pipelines, each with their last 5 SFN runs."""
    pipelines = [{
        "key": "core",
        "name": "Core Disease Surveillance",
        "steps": ["Data Source", "Leader Detection", "Prediction", "Generation", "Delivery"],
        "state_machine": CORE_STATE_MACHINE,
        "recent_runs": _recent_runs(CORE_STATE_MACHINE),
    }]

    for key, (stack_name, sm_name) in PLUGIN_STACKS.items():
        if _describe_stack(stack_name) is None:
            continue  # plugin not deployed
        pipelines.append({
            "key": key,
            "name": stack_name.replace("HealthSignals-", ""),
            "steps": ["Data Source", "Detection", "Generation", "Delivery"],
            "state_machine": sm_name,
            "recent_runs": _recent_runs(sm_name) if sm_name else [],
        })

    return {"pipelines": pipelines}


# --- GET /runs?arn= ----------------------------------------------------------


def _parse_severity(sev_text: str) -> dict:
    """Parse the severity-classification JSON, tolerating markdown ```json fences.

    The model sometimes wraps its JSON in a ```json ... ``` code fence; strip it
    (or extract the first {...} block) before parsing so the dashboard gets a
    clean {severity, reasoning, confidence} object instead of a raw blob.
    """
    text = sev_text.strip()
    if text.startswith("```"):
        # Drop the opening fence line and any trailing fence.
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Last resort: extract the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, TypeError):
                pass
    return {"raw": sev_text}


def _bedrock_text(node: Any) -> str:
    """Extract text from a Bedrock InvokeModel result stored in SFN state."""
    if not isinstance(node, dict):
        return ""
    body = node.get("Body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not isinstance(body, dict):
        return ""
    content = body.get("content") or [{}]
    return content[0].get("text", "") if content else ""


def get_run_detail(execution_arn: str) -> dict:
    """DescribeExecution -> parsed {brief, classification, email, delivery}.

    The brief/classification/email/delivery live only in the SFN execution
    output (no DynamoDB store). Field names differ per pipeline:
      core:     situation_brief_result / severity_result / communication_result
      shortage: shortage_brief_result or combined_brief_result
      outbreak: brief_result / severity_result
    """
    desc = sfn.describe_execution(executionArn=execution_arn)
    status = desc.get("status")
    result: dict = {
        "execution_arn": execution_arn,
        "name": desc.get("name"),
        "status": status,
        "started_at": desc["startDate"].isoformat() if desc.get("startDate") else None,
        "stopped_at": desc["stopDate"].isoformat() if desc.get("stopDate") else None,
    }

    out_raw = desc.get("output")
    if not out_raw:
        # Running or failed-before-output; surface the input + any error.
        result["brief"] = ""
        result["classification"] = None
        result["email"] = ""
        result["delivery"] = None
        if status == "FAILED":
            result["error"] = "Execution failed before producing output."
        return result

    out = json.loads(out_raw)

    # Brief — try each pipeline's field name.
    brief = ""
    for key in ("situation_brief_result", "brief_result",
                "shortage_brief_result", "combined_brief_result"):
        text = _bedrock_text(out.get(key))
        if text:
            brief = text
            break

    # Classification / severity.
    classification = None
    sev_text = _bedrock_text(out.get("severity_result"))
    if sev_text:
        classification = _parse_severity(sev_text)
    elif out.get("severity"):
        classification = {"severity": out.get("severity")}

    # Email — core has a dedicated communication draft; other pipelines derive
    # the email from the brief at dispatch, so fall back to the brief.
    email = _bedrock_text(out.get("communication_result")) or brief

    # Delivery — dispatcher output (recipients).
    delivery = out.get("delivery_result")
    if isinstance(delivery, dict) and "Payload" in delivery:
        delivery = delivery["Payload"]

    result.update({
        "brief": brief,
        "classification": classification,
        "email": email,
        "delivery": delivery,
        "error": out.get("error"),
    })
    return result


# --- helpers -----------------------------------------------------------------


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }
