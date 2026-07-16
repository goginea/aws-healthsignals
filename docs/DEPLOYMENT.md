# Deployment Guide — Amazon HealthSignals

## Prerequisites

- **AWS Account** with admin access
- **AWS CLI** configured (`aws sts get-caller-identity` returns your account)
- **Node.js 20+** and **Python 3.11+**
- **Bedrock Model Access**: Enable Claude Sonnet 4.5 in the [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess)
- **CDK CLI**: `npm install -g aws-cdk` or use `npx aws-cdk`

## Deployment Steps

### Step 1: Install CDK Dependencies

```bash
cd aws-healthsignals/cdk
pip install -r requirements.txt
```

### Step 2: Bootstrap CDK (first time only)

```bash
cdk bootstrap aws://ACCOUNT_ID/us-east-1
```

> If `cdk` is not in your PATH, use `npx aws-cdk bootstrap` or the full path to the CDK binary.

### Step 3: Configure Plugin Modules (optional)

Edit `cdk/cdk.json` to enable or disable optional modules:

```json
{
  "context": {
    "enable_drug_shortage": true
  }
}
```

Set `false` to deploy core-only (7 stacks). Set `true` to include the Drug Shortage Intelligence module (8 stacks).

### Step 4: Deploy All Stacks

```bash
cdk deploy --all --require-approval never
```

**Core stacks (always deployed):**

1. `HealthSignals-Ingestion` — S3 bucket, SQS queues, 3 fetcher Lambdas, EventBridge schedule
2. `HealthSignals-Prediction` — DynamoDB tables, 3 prediction Lambdas
3. `HealthSignals-Generation` — Step Functions state machine, Bedrock IAM
4. `HealthSignals-Orchestration` — Pipeline coordinator, pipeline_runs table, S3 event trigger, EventBridge PutEvents
5. `HealthSignals-Delivery` — SES/SNS, alert dispatcher (registry-based), feedback collector/recalibrator
6. `HealthSignals-Subscription` — API Gateway, subscription Lambdas, Secrets Manager
7. `HealthSignals-Monitoring` — CloudWatch dashboards, alarms, SNS ops topic

**Optional plugin stacks:**

8. `HealthSignals-DrugShortage` — openFDA fetcher, change detector, enrichment Lambda, own Step Functions, DynamoDB tables, alarms, dashboard

### Step 5: Upload Config to S3

```bash
cd aws-healthsignals  # repo root
aws s3 sync config/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/config/
```

### Step 6: Upload Knowledge Base Documents

```bash
aws s3 sync bedrock/knowledge_bases/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/
```

Then create Bedrock Knowledge Bases in the console pointing at these S3 paths:

- CDC Guidelines KB: `s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/cdc_guidelines/`
- Communication Templates KB: `s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/communication_templates/`

### Step 7: Grant Bedrock IAM and Lambda Invoke Permissions

The Step Functions roles need Bedrock model access and permission to invoke the alert dispatcher. CDK logical IDs include a hash suffix — use `list-stack-resources` to find the actual role names.

**Core alert generation state machine:**

```bash
# Find the role name (CDK appends a hash suffix to logical IDs)
ROLE_NAME=$(aws cloudformation list-stack-resources \
  --stack-name HealthSignals-Generation \
  --query "StackResourceSummaries[?starts_with(LogicalResourceId,'BedrockInvocationRole')].PhysicalResourceId" \
  --output text | sed 's|.*/||')

echo "Core SFN Role: $ROLE_NAME"

# Grant Bedrock InvokeModel
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name BedrockInferenceProfileAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": ["*"]
    }]
  }'

# Grant Lambda invoke for alert dispatcher
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name LambdaInvokeDispatcher \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": ["arn:aws:lambda:us-east-1:'$ACCOUNT_ID':function:healthsignals-alert-dispatcher"]
    }]
  }'
```

**Drug Shortage state machine (if module enabled):**

```bash
SHORTAGE_ROLE=$(aws cloudformation list-stack-resources \
  --stack-name HealthSignals-DrugShortage \
  --query "StackResourceSummaries[?starts_with(LogicalResourceId,'ShortageBedrockRole')].PhysicalResourceId" \
  --output text | sed 's|.*/||')

echo "Shortage SFN Role: $SHORTAGE_ROLE"

aws iam put-role-policy \
  --role-name "$SHORTAGE_ROLE" \
  --policy-name BedrockInferenceProfileAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": ["*"]
    }]
  }'

aws iam put-role-policy \
  --role-name "$SHORTAGE_ROLE" \
  --policy-name LambdaInvokeDispatcher \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": ["arn:aws:lambda:us-east-1:'$ACCOUNT_ID':function:healthsignals-alert-dispatcher"]
    }]
  }'
```

### Step 8: Grant S3 Read to Prediction Lambdas

The prediction Lambdas need S3 access to read ingested data and config:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="healthsignals-data-${ACCOUNT_ID}-us-east-1"

for FUNC in healthsignals-leader-detection healthsignals-geographic-affinity healthsignals-timing-estimation; do
  ROLE=$(aws lambda get-function --function-name $FUNC --query 'Configuration.Role' --output text | sed 's|.*/||')
  echo "Granting S3 access to $FUNC (role: $ROLE)"
  aws iam put-role-policy --role-name "$ROLE" --policy-name S3DataRead \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": [
          "arn:aws:s3:::'$BUCKET'",
          "arn:aws:s3:::'$BUCKET'/*"
        ]
      }]
    }'
done
```

### Step 9: Seed Calibration Data

```bash
python scripts/seed_calibration_data.py --seasons 3
```

This backfills 3 seasons of historical lag/severity data from the Delphi API into DynamoDB.

### Step 10: Verify Data Ingestion

```bash
aws lambda invoke --function-name healthsignals-delphi-fetcher \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
```

Should return `statusCode: 200` with 12 signals fetched (3 diseases x 4 metros).

### Step 11: Verify SES Sender

```bash
aws ses verify-email-identity --email-address your-alerts@yourdomain.com
```

Update `alert_sender_email` in `cdk/cdk.json` context to match your verified email, then redeploy the Delivery stack.

---

## Deploying the Drug Shortage Module After Core

If you initially deployed with `enable_drug_shortage: false` and want to add it later:

```bash
# 1. Update cdk.json
#    Set "enable_drug_shortage": true in context

# 2. Deploy the new stack + updated stacks
cdk deploy HealthSignals-DrugShortage HealthSignals-Delivery HealthSignals-Subscription

# 3. Upload shortage-specific config
BUCKET="healthsignals-data-${ACCOUNT_ID}-us-east-1"
aws s3 cp config/data_sources/openfda_shortages.json s3://${BUCKET}/config/data_sources/openfda_shortages.json
aws s3 cp config/shortage_monitoring/therapeutic_categories.json s3://${BUCKET}/config/shortage_monitoring/therapeutic_categories.json
aws s3 cp config/alert_categories.json s3://${BUCKET}/config/alert_categories.json

# 4. Grant Bedrock IAM to the shortage state machine role (see Step 7 above)

# 5. Verify the fetcher works
aws lambda invoke --function-name healthsignals-openfda-shortage-fetcher \
  --payload '{"source": "manual_test"}' --cli-binary-format raw-in-base64-out /dev/stdout
```

The module begins operation on the next Monday 6 AM UTC EventBridge trigger.

---

## Adding a New State After Deployment

No code changes needed — config only:

```bash
cp config/states/_template.json config/states/florida.json
# Edit florida.json with metros + counties
aws s3 cp config/states/florida.json s3://${BUCKET}/config/states/florida.json
python scripts/seed_calibration_data.py --state florida --seasons 3
```

The system auto-discovers new states on the next execution.

---

## Bedrock Models

| Step                    | Model             | Inference Profile ID                           |
| ----------------------- | ----------------- | ---------------------------------------------- |
| Situation Brief         | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Severity Classification | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Preparation Checklist   | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Communication Drafting  | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Shortage Brief (plugin) | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |

All requests include `"thinking": {"type": "disabled"}` to prevent extended thinking blocks that break Step Functions JSONPath references.

---

## Bedrock Guardrail (Recommended)

Create a guardrail to block clinical treatment recommendations:

1. Go to **Amazon Bedrock** > **Guardrails** in the console
2. Create with denied topics: clinical recommendations, diagnostic statements, quarantine orders, vaccination mandates
3. Note the Guardrail ID
4. Update `config/system.json` with the ID

> The guardrail is not wired into the Step Functions ASL automatically. The system relies on prompt-level instructions as the primary safety mechanism. Guardrail integration is a planned enhancement.

---

## End-to-End Testing

```bash
chmod +x scripts/test_end_to_end.sh
./scripts/test_end_to_end.sh
```

**What the script does:**

1. Temporarily lowers the flu threshold to 0.01% AND disables `require_rising_trend`
2. Clears alert state for the test season
3. Forces cold starts on prediction Lambdas (cache invalidation)
4. Invokes the pipeline coordinator
5. Polls Step Functions until completion
6. Reports PASS/FAIL with generated alert preview
7. Restores original config (even on failure)

**Duration:** ~45 seconds. **Cost:** ~$0.10 in Bedrock tokens.

**Important:** The script forces Lambda cold starts by updating an environment variable (`CACHE_BUST`). This ensures the new threshold is picked up immediately. Both the pipeline coordinator and leader_detection Lambda must be cold-started.

**If the test reports "No alerts triggered":**

- Verify Delphi data exists: `aws s3 ls s3://${BUCKET}/raw/delphi/ --recursive | tail -5`
- Check if `require_rising_trend` was properly disabled (trend must be "rising" or config must have `false`)
- Manually invoke leader_detection with test data to isolate the issue (see Troubleshooting)

---

## Troubleshooting

| Symptom                                   | Cause                                          | Fix                                                                                                           |
| ----------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `ConfigLoadError`                         | Config not in S3                               | `aws s3 sync config/ s3://BUCKET/config/`                                                                     |
| `AccessDenied` on S3 ListObjectsV2        | Prediction Lambdas missing S3 perms            | Run Step 8 (S3 grant loop)                                                                                    |
| `Bedrock.AccessDeniedException`           | IAM needs `bedrock:InvokeModel` on `"*"`       | See Step 7                                                                                                    |
| `States.Runtime` JSONPath error           | Thinking blocks in model output                | Ensure `thinking: disabled` in ASL                                                                            |
| CDK says "no changes"                     | Asset hash cached                              | `rm -rf cdk.out` and redeploy                                                                                 |
| `No metro has crossed threshold`          | Lambdas using cached config with old threshold | Force cold start: update any env var on the Lambda                                                            |
| Lambda uses stale config                  | Warm instance cache                            | `aws lambda update-function-configuration --function-name FUNC --environment ...` with a new CACHE_BUST value |
| CDK logical ID not found                  | CDK appends hash suffixes                      | Use `aws cloudformation list-stack-resources` to find actual logical IDs                                      |
| Drug Shortage SFN fails with AccessDenied | Shortage Bedrock role missing IAM              | Run shortage role IAM commands from Step 7                                                                    |
