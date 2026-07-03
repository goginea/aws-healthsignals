# Deployment Guide

## Prerequisites

### AWS Account Requirements
- AWS account with billing enabled
- IAM user/role with CDK deployment permissions
- **Bedrock model access** — request access to:
  - `anthropic.claude-3-haiku-20240307-v1:0`
  - `anthropic.claude-3-5-sonnet-20241022-v2:0`
  - Access is region-specific — request in `us-east-1`
- SES verified sender identity (email address or domain)
- SNS SMS sending capabilities (may require support ticket for production)
- Secrets Manager secret for subscription token signing key

### Local Requirements
- Python 3.11+
- AWS CDK v2 (`npm install -g aws-cdk`)
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
# .venv\Scripts\activate   # Windows

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
cdk deploy --all --require-approval broadening
```

Individual stacks (in dependency order):
```bash
cdk deploy HealthSignals-Ingestion        # S3 + SQS + DLQ + fetcher Lambdas + EventBridge
cdk deploy HealthSignals-Prediction       # DynamoDB tables + prediction Lambdas
cdk deploy HealthSignals-Generation       # Step Functions + Bedrock IAM
cdk deploy HealthSignals-Delivery         # SES/SNS + alert dispatcher
cdk deploy HealthSignals-Monitoring       # CloudWatch dashboards + alarms
cdk deploy HealthSignals-Orchestration    # Pipeline coordinator + S3 trigger
cdk deploy HealthSignals-Subscription     # API Gateway + subscription Lambdas
```

### 4. Upload Config to S3

The config-driven architecture reads from S3 at runtime:

```bash
# From the repo root
aws s3 sync config/ s3://healthsignals-data-ACCOUNT-REGION/config/ \
  --exclude "_template.json"
```

### 5. Create Bedrock Knowledge Bases

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

### 6. Create Token Signing Secret

For the subscription API's HMAC-signed tokens:

```bash
aws secretsmanager create-secret \
  --name healthsignals/token-signing-key \
  --secret-string "$(openssl rand -hex 32)"
```

### 7. Verify SES Sender

```bash
aws ses verify-email-identity --email-address alerts@yourdomain.com
# Check inbox and click verification link
```

### 8. Seed Calibration Data

Populates DynamoDB with 3 seasons of historical lag data from the Delphi API:

```bash
cd scripts
python seed_calibration_data.py --seasons 3 --region us-east-1
```

### 9. Test the Pipeline

```bash
# Trigger manual ingestion
aws lambda invoke --function-name healthsignals-delphi-fetcher \
  --payload '{"source": "manual"}' response.json
cat response.json

# Verify data landed in S3
aws s3 ls s3://healthsignals-data-ACCOUNT-REGION/raw/delphi/ --recursive

# Test orchestration manually
aws lambda invoke --function-name healthsignals-pipeline-coordinator \
  --payload '{"source":"manual","state_key":"texas","disease_key":"influenza","week":"202645"}' \
  orchestration_response.json

# Test subscription API
curl -X POST https://API_ID.execute-api.us-east-1.amazonaws.com/prod/subscribe \
  -H "Content-Type: application/json" \
  -d '{"county_fips":"48143","county_name":"Erath County","state":"texas","contact_name":"Test Officer","contact_email":"test@example.com","diseases":["influenza","rsv","covid"],"delivery_preferences":{"channels":["email"]}}'
```

### 10. Verify End-to-End

```bash
# Check CloudWatch Dashboard
# Navigate: CloudWatch → Dashboards → "HealthSignals-Operations"

# Verify EventBridge rules
aws events list-rules --name-prefix "HealthSignals"

# Check SQS queues are healthy
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/ACCOUNT/healthsignals-ingest-delphi \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# Verify DLQ is empty (no failures)
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/ACCOUNT/healthsignals-ingestion-dlq \
  --attribute-names ApproximateNumberOfMessages
```

## Adding a New State After Deployment

No redeployment needed — config only:

```bash
# 1. Create state config
cp config/states/_template.json config/states/florida.json
# Edit florida.json with metros, counties, contacts

# 2. Upload to S3
aws s3 cp config/states/florida.json \
  s3://healthsignals-data-ACCOUNT-REGION/config/states/florida.json

# 3. Seed calibration data for the new state's metros
python scripts/seed_calibration_data.py --state florida --seasons 3
```

The pipeline coordinator will automatically detect and monitor the new state on its next run.

## Teardown

```bash
cd cdk
cdk destroy --all
```

**Note:** DynamoDB tables and S3 buckets have `RETAIN` removal policy — remove manually:
```bash
aws dynamodb delete-table --table-name healthsignals-county-configs
aws dynamodb delete-table --table-name healthsignals-alert-state
aws dynamodb delete-table --table-name healthsignals-calibration
aws dynamodb delete-table --table-name healthsignals-pipeline-runs
aws dynamodb delete-table --table-name healthsignals-subscriptions
aws s3 rb s3://healthsignals-data-ACCOUNT-REGION --force
```

## Cost Monitoring

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "HealthSignals-BillingAlarm" \
  --metric-name EstimatedCharges \
  --namespace AWS/Billing \
  --statistic Maximum \
  --period 86400 \
  --threshold 75 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --dimensions Name=Currency,Value=USD
```

Expected costs:
- Pilot (5 counties): $5–15/month
- State deployment (100 counties): $61–117/month
- Multi-state (500 counties): $300–550/month
