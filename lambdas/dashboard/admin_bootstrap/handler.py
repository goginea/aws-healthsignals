"""CloudFormation custom-resource: bootstrap the dashboard admin login.

On Create/Update it reads the admin password from Secrets Manager (a plain
string) and sets it as the user's PERMANENT Cognito password via
AdminSetUserPassword. This guarantees the password stored in the secret and the
password set on the user are the SAME value — avoiding the dynamic-reference /
JSON-key pitfalls of passing the secret through CloudFormation into an SDK call.

Reads its inputs from the custom-resource ResourceProperties:
    UserPoolId, Username, SecretArn
"""
import json
import logging
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

sm = boto3.client("secretsmanager")
idp = boto3.client("cognito-idp")


def handler(event, context):
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    physical_id = f"admin-bootstrap-{props.get('Username', 'unknown')}"

    try:
        if request_type in ("Create", "Update"):
            password = sm.get_secret_value(SecretId=props["SecretArn"])["SecretString"]
            idp.admin_set_user_password(
                UserPoolId=props["UserPoolId"],
                Username=props["Username"],
                Password=password,
                Permanent=True,
            )
            logger.info(f"Set permanent password for {props['Username']}")
        # Delete: nothing to undo (leaving the user intact is intentional).
        _send(event, context, "SUCCESS", physical_id)
    except Exception as e:
        logger.exception("admin bootstrap failed")
        _send(event, context, "FAILED", physical_id, reason=str(e))


def _send(event, context, status, physical_id, reason=""):
    """Signal the result back to CloudFormation via the pre-signed S3 URL."""
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": True,
        "Data": {},
    }).encode("utf-8")
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    urllib.request.urlopen(req)  # nosec - CloudFormation-provided pre-signed URL
