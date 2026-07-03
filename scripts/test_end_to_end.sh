#!/bin/bash
#
# Amazon HealthSignals — End-to-End Pipeline Test
#
# Validates the full pipeline: ingestion → leader detection → geographic affinity
# → timing estimation → Bedrock alert generation (4 steps) → delivery dispatch.
#
# What it does:
#   1. Temporarily lowers the flu threshold to 0.01% (so current off-season data triggers)
#   2. Clears any existing alert state for the test season
#   3. Invokes the pipeline coordinator
#   4. Polls Step Functions execution until SUCCEEDED/FAILED/timeout
#   5. Prints results and restores the original threshold
#
# Usage:
#   ./scripts/test_end_to_end.sh
#
# Prerequisites:
#   - AWS CLI configured with valid credentials
#   - HealthSignals stacks deployed (npx aws-cdk deploy --all)
#   - Config uploaded to S3 (aws s3 sync config/ s3://...)
#   - At least one Delphi fetch completed (aws lambda invoke --function-name healthsignals-delphi-fetcher)
#
set -eo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- Derive account/region ---
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Amazon HealthSignals — End-to-End Pipeline Test${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

echo -e "${YELLOW}[1/8] Checking prerequisites...${NC}"

# Get account ID and region
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
    echo -e "${RED}ERROR: AWS CLI not configured or credentials expired.${NC}"
    echo "  Run: aws sts get-caller-identity"
    exit 1
fi

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
BUCKET="healthsignals-data-${ACCOUNT_ID}-${REGION}"
SFN_ARN="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:healthsignals-alert-generation"
CONFIG_KEY="config/diseases/influenza.json"
LAMBDA_NAME="healthsignals-pipeline-coordinator"
ALERT_TABLE="healthsignals-alert-state"

echo "  Account:  ${ACCOUNT_ID}"
echo "  Region:   ${REGION}"
echo "  Bucket:   ${BUCKET}"
echo "  Lambda:   ${LAMBDA_NAME}"

# Check Lambda exists
aws lambda get-function --function-name "$LAMBDA_NAME" > /dev/null 2>&1 || {
    echo -e "${RED}ERROR: Lambda '${LAMBDA_NAME}' not found. Deploy first: npx aws-cdk deploy --all${NC}"
    exit 1
}

# Check config exists in S3
aws s3 ls "s3://${BUCKET}/${CONFIG_KEY}" > /dev/null 2>&1 || {
    echo -e "${RED}ERROR: Config not found at s3://${BUCKET}/${CONFIG_KEY}${NC}"
    echo "  Run: aws s3 sync config/ s3://${BUCKET}/config/"
    exit 1
}

# Check Delphi data exists
DELPHI_DATA=$(aws s3 ls "s3://${BUCKET}/raw/delphi/" 2>/dev/null | head -1)
if [ -z "$DELPHI_DATA" ]; then
    echo -e "${RED}ERROR: No Delphi data in S3. Run the fetcher first:${NC}"
    echo "  aws lambda invoke --function-name healthsignals-delphi-fetcher --payload '{}' /dev/stdout"
    exit 1
fi

echo -e "${GREEN}  ✓ All prerequisites met${NC}"
echo ""

# --- Trap: always restore threshold on exit ---
ORIGINAL_CONFIG=""
cleanup() {
    if [ -n "$ORIGINAL_CONFIG" ] && [ -f "$ORIGINAL_CONFIG" ]; then
        echo ""
        echo -e "${YELLOW}[8/8] Restoring original threshold...${NC}"
        # Restore original (unchanged) config
        aws s3 cp "$ORIGINAL_CONFIG" "s3://${BUCKET}/${CONFIG_KEY}" --quiet
        echo -e "${GREEN}  ✓ Threshold restored to original value${NC}"
        rm -f "$ORIGINAL_CONFIG" /tmp/healthsignals_test_*.json
    fi
}
trap cleanup EXIT

# --- Lower threshold ---
echo -e "${YELLOW}[2/8] Temporarily lowering flu threshold to 0.01%...${NC}"
ORIGINAL_CONFIG="/tmp/healthsignals_test_original.json"
MODIFIED_CONFIG="/tmp/healthsignals_test_modified.json"

aws s3 cp "s3://${BUCKET}/${CONFIG_KEY}" "$ORIGINAL_CONFIG" --quiet
sed 's/"threshold_pct_ed_visits": [0-9.]*/"threshold_pct_ed_visits": 0.01/' "$ORIGINAL_CONFIG" > "$MODIFIED_CONFIG"
aws s3 cp "$MODIFIED_CONFIG" "s3://${BUCKET}/${CONFIG_KEY}" --quiet
echo -e "${GREEN}  ✓ Threshold set to 0.01% (will trigger on any real data)${NC}"
echo ""

# --- Clear alert state ---
echo -e "${YELLOW}[3/8] Clearing alert state for test season...${NC}"
# Delete any existing leader detection for influenza 2026-27 season
# (week 202645 → season 2026-27 because week 45 >= 40)
aws dynamodb delete-item \
    --table-name "$ALERT_TABLE" \
    --key '{"county_fips": {"S": "LEADER_41700"}, "disease_season": {"S": "influenza_2026-27"}}' \
    2>/dev/null || true
aws dynamodb delete-item \
    --table-name "$ALERT_TABLE" \
    --key '{"county_fips": {"S": "LEADER_48201"}, "disease_season": {"S": "influenza_2026-27"}}' \
    2>/dev/null || true
aws dynamodb delete-item \
    --table-name "$ALERT_TABLE" \
    --key '{"county_fips": {"S": "LEADER_48113"}, "disease_season": {"S": "influenza_2026-27"}}' \
    2>/dev/null || true
aws dynamodb delete-item \
    --table-name "$ALERT_TABLE" \
    --key '{"county_fips": {"S": "LEADER_48453"}, "disease_season": {"S": "influenza_2026-27"}}' \
    2>/dev/null || true
aws dynamodb delete-item \
    --table-name "$ALERT_TABLE" \
    --key '{"county_fips": {"S": "LEADER_48029"}, "disease_season": {"S": "influenza_2026-27"}}' \
    2>/dev/null || true

# Force cold start on leader_detection to pick up new threshold
aws lambda update-function-configuration \
    --function-name healthsignals-leader-detection \
    --environment "$(aws lambda get-function-configuration --function-name healthsignals-leader-detection --query 'Environment' --output json | sed 's/"CACHE_BUST":"[^"]*"/"CACHE_BUST":"'$(date +%s)'"/' | if ! grep -q CACHE_BUST; then echo '{}'; else cat; fi)" \
    --query 'FunctionName' --output text > /dev/null 2>&1 || true

sleep 3
echo -e "${GREEN}  ✓ Alert state cleared, Lambda cache busted${NC}"
echo ""

# --- Invoke pipeline ---
echo -e "${YELLOW}[4/8] Invoking pipeline coordinator...${NC}"
PAYLOAD='{"source": "manual", "state_key": "texas", "disease_key": "influenza", "week": "202645"}'
INVOKE_RESULT=$(aws lambda invoke \
    --function-name "$LAMBDA_NAME" \
    --payload "$PAYLOAD" \
    --cli-binary-format raw-in-base64-out \
    /tmp/healthsignals_test_response.json \
    --query 'StatusCode' --output text 2>/dev/null)

RESPONSE=$(cat /tmp/healthsignals_test_response.json)
ALERTS_TRIGGERED=$(echo "$RESPONSE" | python3 -c "import sys,json; r=json.load(sys.stdin); print(json.loads(r.get('body','{}')).get('alerts_triggered', 0))" 2>/dev/null || echo "0")
ERRORS=$(echo "$RESPONSE" | python3 -c "import sys,json; r=json.load(sys.stdin); print(json.loads(r.get('body','{}')).get('errors', []))" 2>/dev/null || echo "[]")

if [ "$ALERTS_TRIGGERED" = "0" ]; then
    echo -e "${RED}  ✗ No alerts triggered!${NC}"
    echo "  Response: $(echo "$RESPONSE" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin),indent=2)[:500])" 2>/dev/null)"
    exit 1
fi

echo -e "${GREEN}  ✓ Pipeline triggered ${ALERTS_TRIGGERED} alert(s)${NC}"
echo ""

# --- Get SFN execution ARN ---
echo -e "${YELLOW}[5/8] Getting Step Functions execution...${NC}"
sleep 2
EXEC_ARN=$(aws stepfunctions list-executions \
    --state-machine-arn "$SFN_ARN" \
    --max-results 1 \
    --query 'executions[0].executionArn' \
    --output text)

if [ -z "$EXEC_ARN" ] || [ "$EXEC_ARN" = "None" ]; then
    echo -e "${RED}  ✗ No Step Functions execution found!${NC}"
    exit 1
fi

EXEC_NAME=$(echo "$EXEC_ARN" | rev | cut -d: -f1 | rev)
echo "  Execution: ${EXEC_NAME}"
echo ""

# --- Poll for completion ---
echo -e "${YELLOW}[6/8] Waiting for Bedrock to generate alert (up to 60s)...${NC}"
MAX_ATTEMPTS=24
POLL_INTERVAL=5
ATTEMPT=0
STATUS="RUNNING"

while [ "$STATUS" = "RUNNING" ] && [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    sleep $POLL_INTERVAL
    STATUS=$(aws stepfunctions describe-execution \
        --execution-arn "$EXEC_ARN" \
        --query 'status' --output text)
    echo "  Attempt ${ATTEMPT}/${MAX_ATTEMPTS}: ${STATUS}"
done

echo ""

# --- Report results ---
echo -e "${YELLOW}[7/8] Results:${NC}"
echo ""

if [ "$STATUS" = "SUCCEEDED" ]; then
    echo -e "${GREEN}  ╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}  ║           ✓ END-TO-END TEST PASSED                      ║${NC}"
    echo -e "${GREEN}  ╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${CYAN}  Pipeline validated:${NC}"
    echo "    ✓ Data ingestion (Delphi API → S3)"
    echo "    ✓ Leader detection (threshold crossing)"
    echo "    ✓ Geographic affinity (county mapping)"
    echo "    ✓ Timing estimation (lag + severity)"
    echo "    ✓ Bedrock Step 1: Situation Brief"
    echo "    ✓ Bedrock Step 2: Severity Classification"
    echo "    ✓ Bedrock Step 3: Preparation Checklist"
    echo "    ✓ Bedrock Step 4: Communication Drafting"
    echo "    ✓ Alert Dispatch"
    echo ""

    # Print excerpt of generated content
    echo -e "${CYAN}  Generated alert preview:${NC}"
    aws stepfunctions describe-execution \
        --execution-arn "$EXEC_ARN" \
        --query 'output' --output text 2>/dev/null | \
        python3 -c "
import sys, json
try:
    output = json.loads(sys.stdin.read())
    brief = output.get('situation_brief_result', {}).get('Body', {}).get('content', [{}])[0].get('text', '')
    print('  ' + brief[:200] + '...')
except:
    print('  (Could not parse output)')
" 2>/dev/null || echo "  (Output not available)"
    echo ""
    EXIT_CODE=0

elif [ "$STATUS" = "FAILED" ]; then
    echo -e "${RED}  ╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}  ║           ✗ END-TO-END TEST FAILED                      ║${NC}"
    echo -e "${RED}  ╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${RED}  Error details:${NC}"
    aws stepfunctions get-execution-history \
        --execution-arn "$EXEC_ARN" \
        --query "events[?type=='TaskFailed'].taskFailedEventDetails.{error:error,cause:cause}" \
        --output json | python3 -c "
import sys, json
events = json.loads(sys.stdin.read())
for e in events:
    print(f\"  Error: {e.get('error', 'unknown')}\")
    cause = e.get('cause', '')
    print(f\"  Cause: {cause[:200]}\")
" 2>/dev/null || echo "  (Could not retrieve error details)"
    echo ""
    echo "  See: aws stepfunctions get-execution-history --execution-arn \"$EXEC_ARN\""
    EXIT_CODE=1

else
    echo -e "${RED}  ✗ Test timed out (status: ${STATUS} after ${MAX_ATTEMPTS} attempts)${NC}"
    echo "  The execution may still be running. Check manually:"
    echo "  aws stepfunctions describe-execution --execution-arn \"$EXEC_ARN\""
    EXIT_CODE=1
fi

# Cleanup happens via trap
exit ${EXIT_CODE}
