"""Unit tests for feedback collector Lambda."""
import json
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(scope="module")
def handler():
    from tests.conftest import load_handler
    return load_handler(
        "delivery/feedback_collector",
        extra_patches={
            "boto3.resource": MagicMock(),
            "boto3.client": MagicMock(),
        },
    )


def _post_event(body: dict) -> dict:
    return {"body": json.dumps(body)}


VALID_FEEDBACK = {
    "alert_id": "influenza_48143_202645",
    "county_fips": "48143",
    "outbreak_occurred": True,
    "timing_accuracy": "on-time",
    "severity_accuracy": "about-right",
    "would_use_again": True,
    "free_text": "Very helpful — pre-ordered flu tests 3 weeks early.",
}


class TestApiResponse:
    """Test API Gateway response formatting."""

    def test_200_response_format(self, handler):
        resp = handler.api_response(200, {"message": "ok"})
        assert resp["statusCode"] == 200
        assert resp["headers"]["Content-Type"] == "application/json"
        body = json.loads(resp["body"])
        assert body["message"] == "ok"

    def test_cors_header_present(self, handler):
        resp = handler.api_response(200, {})
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


class TestFeedbackHandlerValidation:
    """Test input validation."""

    def test_valid_feedback_returns_200(self, handler):
        with patch.object(handler, "feedback_table"), \
             patch.object(handler, "check_and_trigger_recalibration", return_value=False):
            result = handler.lambda_handler(_post_event(VALID_FEEDBACK), None)
        assert result["statusCode"] == 200

    def test_missing_alert_id_returns_400(self, handler):
        body = {**VALID_FEEDBACK}
        del body["alert_id"]
        with patch.object(handler, "feedback_table"):
            result = handler.lambda_handler(_post_event(body), None)
        assert result["statusCode"] == 400
        assert "alert_id" in json.loads(result["body"])["error"]

    def test_missing_county_fips_returns_400(self, handler):
        body = {**VALID_FEEDBACK}
        del body["county_fips"]
        with patch.object(handler, "feedback_table"):
            result = handler.lambda_handler(_post_event(body), None)
        assert result["statusCode"] == 400

    def test_invalid_json_returns_400(self, handler):
        result = handler.lambda_handler({"body": "{{not json}}"}, None)
        assert result["statusCode"] == 400

    def test_optional_fields_not_required(self, handler):
        """Feedback with only required fields should succeed."""
        minimal = {"alert_id": "flu_48143_202645", "county_fips": "48143"}
        with patch.object(handler, "feedback_table"), \
             patch.object(handler, "check_and_trigger_recalibration", return_value=False):
            result = handler.lambda_handler(_post_event(minimal), None)
        assert result["statusCode"] == 200

    def test_feedback_stored_to_dynamodb(self, handler):
        with patch.object(handler, "feedback_table") as mock_table, \
             patch.object(handler, "check_and_trigger_recalibration", return_value=False):
            handler.lambda_handler(_post_event(VALID_FEEDBACK), None)
            mock_table.put_item.assert_called_once()
            stored = mock_table.put_item.call_args[1]["Item"]
        assert stored["alert_id"] == VALID_FEEDBACK["alert_id"]
        assert stored["county_fips"] == VALID_FEEDBACK["county_fips"]
        assert "submitted_at" in stored

    def test_recalibration_triggered_in_response(self, handler):
        with patch.object(handler, "feedback_table"), \
             patch.object(handler, "check_and_trigger_recalibration", return_value=True):
            result = handler.lambda_handler(_post_event(VALID_FEEDBACK), None)
        body = json.loads(result["body"])
        assert body.get("recalibration") == "triggered"

    def test_internal_error_returns_500(self, handler):
        with patch.object(handler, "feedback_table"), \
             patch.object(handler, "check_and_trigger_recalibration",
                          side_effect=Exception("DDB error")):
            result = handler.lambda_handler(_post_event(VALID_FEEDBACK), None)
        assert result["statusCode"] == 500

    def test_direct_dict_body_accepted(self, handler):
        """Lambda invoked directly with dict body (not API Gateway string) should work."""
        with patch.object(handler, "feedback_table"), \
             patch.object(handler, "check_and_trigger_recalibration", return_value=False):
            result = handler.lambda_handler({"body": VALID_FEEDBACK}, None)
        assert result["statusCode"] == 200


class TestCheckAndTriggerRecalibration:
    """Test recalibration threshold logic."""

    def test_below_threshold_no_trigger(self, handler):
        with patch.object(handler, "feedback_table") as mock_table, \
             patch.object(handler, "invoke_recalibrator") as mock_invoke:
            mock_table.query.return_value = {"Count": 2}  # Below threshold of 3
            result = handler.check_and_trigger_recalibration("flu_48143_202645")
        assert result is False
        mock_invoke.assert_not_called()

    def test_at_threshold_triggers_once(self, handler):
        with patch.object(handler, "feedback_table") as mock_table, \
             patch.object(handler, "invoke_recalibrator") as mock_invoke:
            mock_table.query.return_value = {"Count": 3}  # Exactly at threshold
            result = handler.check_and_trigger_recalibration("flu_48143_202645")
        assert result is True
        mock_invoke.assert_called_once_with("flu_48143_202645")

    def test_above_threshold_no_re_trigger(self, handler):
        with patch.object(handler, "feedback_table") as mock_table, \
             patch.object(handler, "invoke_recalibrator") as mock_invoke:
            mock_table.query.return_value = {"Count": 5}  # Already triggered
            result = handler.check_and_trigger_recalibration("flu_48143_202645")
        assert result is False
        mock_invoke.assert_not_called()

    def test_dynamo_error_returns_false(self, handler):
        with patch.object(handler, "feedback_table") as mock_table:
            mock_table.query.side_effect = Exception("DynamoDB down")
            result = handler.check_and_trigger_recalibration("flu_48143_202645")
        assert result is False  # Fail-safe: don't break feedback on recal error


class TestInvokeRecalibrator:
    """Test async Lambda invocation."""

    def test_invokes_async(self, handler):
        with patch.object(handler, "lambda_client") as mock_lambda:
            handler.invoke_recalibrator("flu_48143_202645")
            call_kwargs = mock_lambda.invoke.call_args[1]
        assert call_kwargs["InvocationType"] == "Event"
        payload = call_kwargs["Payload"]
        payload_str = payload.decode() if isinstance(payload, bytes) else str(payload)
        assert "flu_48143_202645" in payload_str

    def test_invocation_failure_does_not_raise(self, handler):
        with patch.object(handler, "lambda_client") as mock_lambda:
            mock_lambda.invoke.side_effect = Exception("Lambda not found")
            # Should not raise — recalibration is best-effort
            handler.invoke_recalibrator("flu_48143_202645")
