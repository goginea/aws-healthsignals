# Deployment Guide

## Prerequisites

### AWS Account Requirements
- AWS account with billing enabled
- IAM user/role with CDK deployment permissions
- **Bedrock model access** — request access in Bedrock Console → Model Access:
  - Claude Sonnet 4.5 (`anthropic.claude-sonnet-4-5-20250929-v1:0`)
  - Claude Sonnet 5 (`anthropic.claude-sonnet-5`)
  - Access is region-specific — request in `us-east-1`
  - ⚠️ Models must be **active** (not Legacy). Check: `aws bedrock list-foundation-models --by-provider Anthropic`
- SES verified sender identity (email address or domain)
- SNS SMS sending capabilities (may require support ticket for production)

### Local Requirements
- Python 3.11+
- Node.js 20+ (for CDK CLI)
- AWS CDK v2 (`npm install -g aws-cdk` or use `npx aws-cdk`)
- AWS CLI configured (`aws configure`)
- Git

## Step-by-Step Deployment

### 1. Clone and Setup

```bash
git clone https://github.com/goginea/aws-healthsignals.git
cd aws-healthsignals/cdk

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

### 2. Bootstrap CDK (first time only)

```bash
cdk bootstrap aws://ACCOUNT_ID/us-east-1
```

### 3. Deploy All 7 Stacks

```bash
# Deploy all stacks with dependency ordering
npx aws-cdk deploy --all --require-approval never
```

Individual stacks (in dependency order):
```bash
npx aws-cdk deploy HealthSignals-Ingestion        # S3 + SQS + DLQ + fetcher Lambdas + EventBridge
npx aws-cdk deploy HealthSignals-Prediction       # DynamoDB tables + prediction Lambdas
npx aws-cdk deploy HealthSignals-Generation       # Step Functions + Bedrock IAM
npx aws-cdk deploy HealthSignals-Orchestration    # Pipeline coordinator + S3 trigger
npx aws-cdk deploy HealthSignals-Delivery         # SES/SNS + alert dispatcher
npx aws-cdk deploy HealthSignals-Subscription     # API Gateway + subscription Lambdas
npx aws-cdk deploy HealthSignals-Monitoring       # CloudWatch dashboards + alarms
```

### 4. Upload Config to S3 (CRITICAL — do this BEFORE invoking any Lambda)

⚠️ **All Lambdas will fail with `ConfigLoadError` if config is not in S3.**

```bash
# From the repo root (not cdk/)
cd ~/Documents/dev/aws-healthsignals
aws s3 sync config/ s3://healthsignals-data-ACCOUNT-REGION/config/ \
  --exclude "_template.json" --exclude "*.pyc" --exclude "__pycache__/*"
```

### 5. Grant Bedrock IAM Permissions for Inference Profiles

⚠️ **The CDK-deployed IAM policy may not cover cross-region inference profiles.**
Inference profiles (model IDs starting with `us.`) route to multiple regions. The Step Functions
role needs `bedrock:InvokeModel` on `"*"` to support cross-region routing:

```bash
# Find the SFN role name
ROLE_NAME=$(aws cloudformation describe-stack-resources \
  --stack-name HealthSignals-Generation \
  --query "StackResources[?LogicalResourceId=='BedrockInvocationRole'].PhysicalResourceId" \
  --output text)

# Grant broad Bedrock access (required for cross-region inference profiles)
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name BedrockInferenceProfileAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": ["*"]
    }]
  }'

# Also grant the SFN role permission to invoke the alert dispatcher Lambda
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name LambdaInvokeDispatcher \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": ["arn:aws:lambda:us-east-1:ACCOUNT:function:healthsignals-alert-dispatcher"]
    }]
  }'
```

### 6. Create Bedrock Knowledge Bases

Upload KB documents to S3 and create Knowledge Bases in the Bedrock console:

```bash
# CDC Guidelines KB
aws s3 mb s3://healthsignals-kb-cdc-guidelines-ACCOUNT
aws s3 sync bedrock/knowledge_bases/cdc_guidelines/ \
  s3://healthsignals-kb-cdc-guidelines-ACCOUNT/ --exclude "README.md"

# Communication Templates KB
aws s3 mb s3://healthsignals-kb-comms-templates-ACCOUNT
aws s3 sync bedrock/knowledge_bases/communication_templates/ \
  s3://healthsignals-kb-comms-templates-ACCOUNT/ --exclude "README.md"
```

Then in Bedrock Console → Knowledge Bases → Create:
1. **CDC Guidelines KB**: Name `healthsignals-cdc-guidelines`, semantic chunking, Titan Embeddings V2
2. **Communication Templates KB**: Name `healthsignals-communication-templates`, fixed chunking (1000 tokens, 200 overlap), Titan Embeddings V2

Update `config/system.json` with the Knowledge Base IDs and re-upload to S3.

### 7. Create Token Signing Secret

For the subscription API's HMAC-signed tokens:

```bash
aws secretsmanager create-secret \
  --name healthsignals/token-signing-key \
  --secret-string "$(openssl rand -hex 32)"
```

### 8. Verify SES Sender

```bash
aws ses verify-email-identity --email-address alerts@yourdomain.com
# Check inbox and click verification link
```

### 9. Seed Calibration Data

Populates DynamoDB with 3 seasons of historical lag data from the Delphi API:

```bash
cd scripts
python seed_calibration_data.py --seasons 3 --region us-east-1
```

⚠️ **Note:** The Delphi API uses `geo_type=county` and `time_type=week` (epiweek format YYYYWW).
If the script returns 0 records, verify the API is accessible: `curl "https://api.delphi.cmu.edu/epidata/covidcast/?data_source=nssp&signal=pct_ed_visits_influenza&geo_type=county&geo_value=48201&time_type=week&time_values=202439-202524"`

### 10. Test the Pipeline

```bash
# Trigger manual ingestion
aws lambda invoke --function-name healthsignals-delphi-fetcher \
  --payload '{}' /dev/stdout

# Verify data landed in S3
aws s3 ls s3://healthsignals-data-ACCOUNT-REGION/raw/delphi/ --recursive

# Test full pipeline (create payload file first)
cat > /tmp/test-payload.json << 'EOF'
{"source":"manual","state_key":"texas","disease_key":"influenza","week":"202645"}
EOF

aws lambda invoke --function-name healthsignals-pipeline-coordinator \
  --payload fileb:///tmp/test-payload.json /dev/stdout
```

### 11. Verify End-to-End

```bash
# Check CloudWatch Dashboard
# Navigate: CloudWatch → Dashboards → "HealthSignals-Operations"

# Verify EventBridge rules
aws events list-rules --name-prefix "HealthSignals"

# Check Step Functions executions
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:ACCOUNT:stateMachine:healthsignals-alert-generation \
  --max-results 5 --query 'executions[].{name:name,status:status}'
```

## Adding a New State After Deployment

No redeployment needed — config only:

```bash
# 1. Create state config
cp config/states/_template.json config/states/florida.json
# Edit florida.json with metros (include primary_county_fips for each), counties, contacts

# 2. Upload to S3
aws s3 cp config/states/florida.json \
  s3://healthsignals-data-ACCOUNT-REGION/config/states/florida.json

# 3. Seed calibration data for the new state's metros
python scripts/seed_calibration_data.py --state florida --seasons 3
```

The pipeline coordinator will automatically detect and monitor the new state on its next run.

## Lambda Layer Structure

Shared code (config_loader, token_utils) is deployed as a Lambda Layer:

```
layers/shared/python/shared/
├── __init__.py
├── config_loader.py
└── token_utils.py
```

This follows Python Lambda Layer convention: files under `python/` are automatically on Lambda's path at `/opt/python/`. Handlers import via `from shared.config_loader import ...`.

The source at `lambdas/shared/` is for **local development/testing only** — the deployed Lambda uses the Layer.

## Troubleshooting

### "ConfigLoadError: Failed to load s3://..."
- **Cause:** Config not uploaded to S3, or Lambda doesn't have S3 read permission
- **Fix:** Run `aws s3 sync config/ s3://healthsignals-data-ACCOUNT-REGION/config/`
- Verify Lambda role has `s3:GetObject` on the bucket

### CDK says "no changes" but handler code was modified
- **Cause:** CDK caches asset hashes. Local file timestamp changes don't always trigger repackaging.
- **Fix:** `rm -rf cdk.out && npx aws-cdk deploy STACK_NAME`
- Alternative: update Lambda directly: `zip -r /tmp/fn.zip handler.py && aws lambda update-function-code --function-name NAME --zip-file fileb:///tmp/fn.zip`

### "Model is marked by provider as Legacy"
- **Cause:** Using an old model ID that Anthropic has deprecated
- **Fix:** Update ASL to use current inference profile IDs (`us.anthropic.claude-sonnet-4-5-*` or `us.anthropic.claude-sonnet-5`)
- Check available models: `aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].{id:inferenceProfileId,status:status}"`

### "Invocation of model ID with on-demand throughput isn't supported"
- **Cause:** Using a foundation model ID directly instead of an inference profile
- **Fix:** Prefix model IDs with `us.` (e.g., `us.anthropic.claude-sonnet-4-5-20250929-v1:0`)

### "bedrock:InvokeModel AccessDeniedException" on us-east-2 or other region
- **Cause:** Cross-region inference profiles route to multiple regions. IAM policy only allows us-east-1.
- **Fix:** Grant `bedrock:InvokeModel` on `"*"` (required for cross-region inference profile routing)

### Lambda uses cached/stale config
- **Cause:** Config loader caches configs in Lambda memory (warm start reuse)
- **Fix:** Force cold start by updating any environment variable:
  ```bash
  aws lambda update-function-configuration --function-name FUNCTION_NAME \
    --environment '{"Variables":{"CACHE_BUST":"2",...existing vars...}}'
  ```

### "No metro signals available" in pipeline coordinator
- **Cause:** S3 data files are keyed by county FIPS (e.g., `48201.json`) but coordinator was looking for MSA code
- **Fix:** Ensure state config has `primary_county_fips` for each metro, and coordinator uses it for S3 lookup

### Step Functions "States.Runtime" error on CommunicationDrafting
- **Cause:** Newer Claude models return extended thinking blocks (`content[0].type = "thinking"`) which break JSONPath `$.content[0].text`
- **Fix:** Add `"thinking": {"type": "disabled"}` to all InvokeModel request bodies in the ASL

## Teardown

```bash
cd cdk
npx aws-cdk destroy --all
```

**Note:** DynamoDB tables and S3 buckets have `RETAIN` removal policy — remove manually:
```bash
aws dynamodb delete-table --table-name healthsignals-county-configs
aws dynamodb delete-table --table-name healthsignals-alert-state
aws dynamodb delete-table --table-name healthsignals-calibration
aws dynamodb delete-table --table-name healthsignals-pipeline-runs
aws dynamodb delete-table --table-name healthsignals-subscriptions
aws dynamodb delete-table --table-name healthsignals-feedback
aws s3 rb s3://healthsignals-data-ACCOUNT-REGION --force
```

## Cost Monitoring

Set up a Cost Allocation Tag (`project: healthsignals`) and create a Cost Explorer filter.
Expected monthly cost for 100 counties: $150–300/month (primarily Bedrock token costs with Sonnet 4.5).
