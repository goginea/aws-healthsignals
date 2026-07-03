# Deployment Guide — Amazon HealthSignals

## Prerequisites

- **AWS Account** with admin access
- **AWS CLI** configured (`aws sts get-caller-identity` returns your account)
- **Node.js 20+** and **Python 3.11+**
- **Bedrock Model Access**: Enable Claude Sonnet 4.5 and Claude Sonnet 5 in the [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess)
- **CDK CLI**: `npm install -g aws-cdk` or use `npx aws-cdk`

## Deployment Steps

### Step 1: Install CDK Dependencies

```bash
cd aws-healthsignals/cdk
pip install -r requirements.txt
```

### Step 2: Bootstrap CDK (first time only)

```bash
npx aws-cdk bootstrap aws://ACCOUNT_ID/us-east-1
```

### Step 3: Deploy All Stacks

```bash
npx aws-cdk deploy --all --require-approval never
```

This deploys 7 stacks in dependency order:
1. `HealthSignals-Ingestion` — S3 bucket, SQS queues, 3 fetcher Lambdas, EventBridge schedule
2. `HealthSignals-Prediction` — DynamoDB tables, 3 prediction Lambdas
3. `HealthSignals-Generation` — Step Functions state machine, Bedrock IAM
4. `HealthSignals-Orchestration` — Pipeline coordinator, pipeline_runs table, S3 event trigger
5. `HealthSignals-Delivery` — SES/SNS, alert dispatcher, feedback collector/recalibrator
6. `HealthSignals-Subscription` — API Gateway, 5 subscription Lambdas, Secrets Manager
7. `HealthSignals-Monitoring` — CloudWatch dashboards, alarms, SNS ops topic

### Step 4: Upload Config to S3 (CRITICAL — must be done before any Lambda invocation)

```bash
cd aws-healthsignals  # repo root
aws s3 sync config/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/config/
```

### Step 5: Upload Knowledge Base Documents

```bash
aws s3 sync bedrock/knowledge_bases/ s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/
```

Then create Bedrock Knowledge Bases in the console pointing at these S3 paths:
- CDC Guidelines KB → `s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/cdc_guidelines/`
- Communication Templates KB → `s3://healthsignals-data-ACCOUNT_ID-us-east-1/knowledge_bases/communication_templates/`

### Step 6: Grant Bedrock IAM for Inference Profiles

Cross-region inference profiles route to multiple AWS regions. The Step Functions role needs broad Bedrock access:

```bash
# Get the SFN role name
ROLE_NAME=$(aws cloudformation describe-stack-resource \
  --stack-name HealthSignals-Generation \
  --logical-resource-id BedrockInvocationRole \
  --query 'StackResourceDetail.PhysicalResourceId' --output text | sed 's|.*/||')

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
```

Also grant the SFN role permission to invoke the alert dispatcher:

```bash
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

### Step 7: Grant S3 Read to Prediction Lambdas

The prediction Lambdas read config from S3:

```bash
for FUNC in healthsignals-leader-detection healthsignals-geographic-affinity healthsignals-timing-estimation; do
  ROLE=$(aws lambda get-function --function-name $FUNC --query 'Configuration.Role' --output text | sed 's|.*/||')
  aws iam put-role-policy --role-name "$ROLE" --policy-name S3ConfigRead \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject","s3:ListBucket"], "Resource": ["arn:aws:s3:::healthsignals-data-'$ACCOUNT_ID'-us-east-1","arn:aws:s3:::healthsignals-data-'$ACCOUNT_ID'-us-east-1/*"]}]
    }'
done
```

### Step 8: Seed Calibration Data

```bash
python scripts/seed_calibration_data.py --seasons 3
```

This backfills 3 seasons of historical lag/severity data from the Delphi API into DynamoDB.

### Step 9: Verify Data Ingestion

```bash
aws lambda invoke --function-name healthsignals-delphi-fetcher --payload '{}' /dev/stdout
```

Should return `statusCode: 200` with 12 signals fetched (3 diseases × 4 metros).

### Step 10: Verify SES Sender (for email delivery)

```bash
aws ses verify-email-identity --email-address your-alerts@yourdomain.com
```

---

## Adding a New State After Deployment

No code changes needed — just config:

```bash
# 1. Create state config (copy template, fill in metros + counties)
cp config/states/_template.json config/states/florida.json
# Edit florida.json with FL metros, counties, contacts

# 2. Upload to S3
aws s3 cp config/states/florida.json s3://healthsignals-data-ACCOUNT_ID-us-east-1/config/states/florida.json

# 3. Seed calibration data for the new state
python scripts/seed_calibration_data.py --seasons 3
```

The system auto-discovers new states on the next execution (config is loaded at Lambda cold start).

---

## Lambda Layer Structure

The shared utilities (config_loader, token_utils) are deployed as a Lambda Layer:

```
layers/shared/
└── python/
    └── shared/
        ├── __init__.py
        ├── config_loader.py
        └── token_utils.py
```

In Lambda runtime, files are at `/opt/python/shared/`. Import with: `from shared.config_loader import ...`

---

## Bedrock Models

| Step | Model | Inference Profile ID |
|------|-------|---------------------|
| Situation Brief | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Severity Classification | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Preparation Checklist (routine) | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Preparation Checklist (high-severity) | Claude Sonnet 5 | `us.anthropic.claude-sonnet-5` |
| Communication Drafting | Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |

All requests include `"thinking": {"type": "disabled"}` to prevent extended thinking blocks that break Step Functions JSONPath references.

---


## Step 7: Create Bedrock Guardrail (Recommended)

The system is designed to block clinical treatment recommendations and diagnostic statements from AI-generated alerts. To activate this:

1. Go to **Amazon Bedrock** → **Guardrails** in the console
2. Create a new guardrail with:
   - **Name:** `healthsignals-clinical-blocker`
   - **Denied Topics:**
     - Clinical treatment recommendations (prescribing drugs, dosages)
     - Diagnostic statements (confirming infections, making diagnoses)
     - Quarantine/isolation orders (legal actions)
     - Vaccination mandates (policy decisions)
   - **Word Filters:** "prescribe", "administer [drug]", "confirmed cases", "diagnosis is"
   - **Content Policy:** Block outputs that could be interpreted as medical advice
3. Note the **Guardrail ID** (format: `abcdefgh1234`)
4. Update `config/system.json`:
   ```json
   "bedrock": {
     "guardrail_id": "YOUR_GUARDRAIL_ID",
     "guardrail_version": "DRAFT"
   }
   ```
5. Upload updated config: `aws s3 cp config/system.json s3://healthsignals-data-ACCOUNT_ID-us-east-1/config/system.json`

> **Note:** The guardrail is NOT wired into the Step Functions ASL automatically — it requires adding `GuardrailIdentifier` and `GuardrailVersion` parameters to each InvokeModel state. This is a planned enhancement. For now, the system relies on prompt-level instructions ("Never recommend clinical treatments") as the primary safety mechanism.

Reference config: `bedrock/guardrails/healthsignals_guardrail.json`


## Troubleshooting

### `ConfigLoadError: Failed to load s3://...`
Lambda can't read config from S3. Either:
- Config not uploaded: `aws s3 sync config/ s3://BUCKET/config/`
- Lambda role lacks S3 read: Add `s3:GetObject` + `s3:ListBucket` permission

### CDK says "no changes" after modifying handler code
CDK caches asset hashes. Force refresh:
```bash
rm -rf cdk.out
npx aws-cdk deploy STACK_NAME --require-approval never
```
If still cached, update Lambda directly:
```bash
cd lambdas/PATH/TO/HANDLER
zip -r /tmp/handler.zip handler.py requirements.txt
aws lambda update-function-code --function-name FUNCTION_NAME --zip-file fileb:///tmp/handler.zip
```

### `Bedrock.ResourceNotFoundException: Model is marked as Legacy`
The model ID is deprecated. Update to current inference profile IDs in `stepfunctions/alert_generation.asl.json`. Check available models:
```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?status=='ACTIVE'].inferenceProfileId"
```

### `Bedrock.ValidationException: Retry with inference profile`
Newer models require inference profile format (`us.anthropic.claude-*`), not direct model IDs (`anthropic.claude-*`).

### `Bedrock.AccessDeniedException` on cross-region resource
Inference profiles route to multiple regions. IAM must grant `bedrock:InvokeModel` on `"*"` (can't enumerate all region-specific ARNs).

### `States.Runtime` error in Step Functions (JSONPath)
Model returned extended thinking blocks. Ensure all InvokeModel steps have `"thinking": {"type": "disabled"}` in the request body.

### Lambda uses stale config (cached from previous invocation)
Force a cold start by updating any environment variable:
```bash
aws lambda update-function-configuration --function-name FUNC_NAME --environment '{"Variables":{"CACHE_BUST":"'$(date +%s)'"}}'
```

### `No metro signals available` in pipeline coordinator
The coordinator reads data from S3 using `primary_county_fips` from state config. Ensure:
1. Delphi fetcher has run at least once
2. State config has `primary_county_fips` for each metro
3. S3 keys match: `raw/delphi/nssp/pct_ed_visits_influenza/YYYY/WXX/COUNTY_FIPS.json`

---

## End-to-End Testing

After deployment, validate the full pipeline with one command:

```bash
chmod +x scripts/test_end_to_end.sh
./scripts/test_end_to_end.sh
```

This script:
1. Temporarily lowers the flu threshold to 0.01% (triggers on any real data)
2. Clears alert state to allow a fresh detection
3. Invokes the pipeline coordinator
4. Polls Step Functions until Bedrock completes all 4 generation steps
5. Reports PASS/FAIL with alert content preview
6. **Automatically restores** the original threshold (even on failure)

**Duration:** ~45 seconds  
**Cost:** ~$0.10 in Bedrock tokens (one Sonnet 4.5 + one Sonnet 5 invocation)

**What it validates:**
- ✅ Data ingestion (reads from S3 data lake)
- ✅ Leader detection (threshold crossing with config-driven thresholds)
- ✅ Geographic affinity (county mapping from state config)
- ✅ Timing estimation (lag/severity from calibration data)
- ✅ Bedrock AI generation (situation brief → severity → checklist → communications)
- ✅ Alert dispatch (invokes delivery Lambda)

**Common test failure causes:**
- `ConfigLoadError`: Config not uploaded → `aws s3 sync config/ s3://BUCKET/config/`
- `Bedrock.AccessDeniedException`: IAM needs `bedrock:InvokeModel` on `"*"`
- `No alerts triggered`: No Delphi data in S3 → run fetcher first
- `States.Runtime` JSONPath error: Thinking blocks → ensure `thinking: disabled` in ASL
