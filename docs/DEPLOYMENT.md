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

### Step 3: Configure Plugin Modules and Sender Email

Edit `cdk/cdk.json` to configure:

```json
{
  "context": {
    "enable_drug_shortage": true,
    "alert_sender_email": "your-verified-sender@yourdomain.com"
  }
}
```

- `enable_drug_shortage`: Set `false` for core-only (7 stacks), `true` to include Drug Shortage (8 stacks)
- `alert_sender_email`: **Must be a verified SES identity** (email or domain). Alerts will not deliver without this. Verify it in Step 11.
- `enable_cdc_outbreak_alerts`: Set `true` to include CDC Outbreak Alerts module
- `enable_forecast_providers`: Set `true` to include Forecast Provider module (FluSight + RSV Hub + custom models)
- `dashboard_admin_email`: Email for the admin dashboard login. If set, an initial Cognito admin user is created and a password is generated into Secrets Manager on deploy. If omitted, the dashboard still deploys but has **no login** until you redeploy with this value set. See [Admin Dashboard](#admin-dashboard) below. This is best passed as a CLI flag (`-c dashboard_admin_email=...`) rather than committed to `cdk.json`.

### Step 4: Deploy All Stacks

```bash
cdk deploy --all --require-approval never -c dashboard_admin_email=you@example.com
```

> Omit `-c dashboard_admin_email=...` and every stack still deploys, including the dashboard — but the dashboard will have no admin account and no one can log in. You can add it later by redeploying just the dashboard stack (see [Admin Dashboard](#admin-dashboard)).

**Core stacks (always deployed):**

1. `HealthSignals-Ingestion` — S3 bucket, SQS queues, 3 fetcher Lambdas, EventBridge schedule
2. `HealthSignals-Prediction` — DynamoDB tables, 3 prediction Lambdas
3. `HealthSignals-Generation` — Step Functions state machine, Bedrock IAM
4. `HealthSignals-Orchestration` — Pipeline coordinator, pipeline_runs table, S3 event trigger, EventBridge PutEvents
5. `HealthSignals-Delivery` — SES/SNS, alert dispatcher (registry-based), feedback collector/recalibrator
6. `HealthSignals-Subscription` — API Gateway, subscription Lambdas, Secrets Manager
7. `HealthSignals-Monitoring` — CloudWatch dashboards, alarms, SNS ops topic
8. `HealthSignals-Dashboard` — Admin web console (CloudFront + private S3, Cognito auth, read-only API); always deployed, login provisioned only when `dashboard_admin_email` is set

**Optional plugin stacks:**

9. `HealthSignals-DrugShortage` — openFDA fetcher, change detector, enrichment Lambda, own Step Functions, DynamoDB tables, alarms, dashboard
10. `HealthSignals-CDCOutbreaks` — CDC RSS fetcher, outbreak processor, own Step Functions, DynamoDB table, alarms, dashboard
11. `HealthSignals-ForecastProviders` — FluSight/RSV Hub fetchers, custom model fetcher, forecast aggregator, DynamoDB table, alarms, dashboard

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

> **SES Sandbox Note:** New AWS accounts start in SES sandbox mode. In sandbox, you can only send emails to verified addresses (both sender AND recipient must be verified). To test delivery, verify the recipient email too:
>
> ```bash
> aws ses verify-email-identity --email-address recipient@example.com
> ```
>
> For production use, request SES production access in the AWS console (SES > Account dashboard > Request production access). This removes the recipient verification requirement.

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

## Deploying the Forecast Provider Module After Core

```bash
# 1. Set "enable_forecast_providers": true in cdk/cdk.json

# 2. Deploy the new stack + updated prediction stack
cdk deploy HealthSignals-ForecastProviders HealthSignals-Prediction

# 3. Upload forecast provider configs
BUCKET="healthsignals-data-${ACCOUNT_ID}-us-east-1"
aws s3 sync config/forecast_providers/ s3://${BUCKET}/config/forecast_providers/

# 4. Grant Bedrock IAM (not needed — this plugin has no Step Functions of its own)

# 5. Verify the FluSight fetcher works
aws lambda invoke --function-name healthsignals-flusight-forecast-fetcher \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
```

The module begins fetching forecasts on the next Wednesday (10 AM UTC).

---

## Admin Dashboard

`HealthSignals-Dashboard` is an admin-only web console served over CloudFront (private S3 origin via OAC) with a read-only API behind API Gateway. Every API route requires a valid Cognito ID token — the dashboard is **not** public. It shows live stack status, the core pipeline and plugin pipelines, recent Step Functions runs, and the Bedrock-generated brief/classification/email per run.

The stack is **always deployed** as part of `cdk deploy --all`. It is fully self-deploying: on deploy it uploads its own frontend to the site bucket, injects the API URL / region / Cognito client ID into a generated `config.js`, and invalidates the CloudFront cache. There are no manual upload steps.

### Provisioning the admin login

A login is provisioned only when you supply an admin email via CDK context:

```bash
cdk deploy HealthSignals-Dashboard --require-approval never \
  -c dashboard_admin_email=you@example.com
```

When `dashboard_admin_email` is set, the stack:

1. Creates an initial Cognito admin user with that email.
2. Generates a strong password into Secrets Manager at `healthsignals/dashboard-admin-password`.
3. Runs a custom resource that sets that password as the user's **permanent** password — so login works immediately, with no Cognito verification email required.

If you deploy **without** `dashboard_admin_email`, the dashboard site and API still deploy, but no admin user, password secret, or login exists. Add one at any time by redeploying just this stack with the flag set — no other stacks are affected.

### Logging in

Read the stack outputs and the generated password:

```bash
# Site URL, API URL, Cognito IDs
aws cloudformation describe-stacks --stack-name HealthSignals-Dashboard \
  --query "Stacks[0].Outputs" --output table

# Initial admin password
aws secretsmanager get-secret-value \
  --secret-id healthsignals/dashboard-admin-password \
  --query SecretString --output text
```

Open the `DashboardUrl` value in a browser and sign in with your admin email and that password. Change the password after first login (standard Cognito account settings).

### Notes

- The password secret uses a `RETAIN` removal policy on the site bucket, so tearing down the stack does not silently drop your dashboard content bucket. See [TEARDOWN.md](TEARDOWN.md).
- The dashboard API is read-only (CloudFormation `Describe*`, Step Functions `List/Describe`, DynamoDB `Query/Scan/GetItem`). The one write-ish action is on-demand drift detection (`DetectStackDrift`), triggered per stack by an explicit button.
- Passing the email as a CLI flag keeps a personal address out of source control. If you prefer, you can set `dashboard_admin_email` in `cdk/cdk.json` context instead, but avoid committing a real personal email to a shared repo.

---

## Adding a New State After Deployment

No code changes and no new config files needed — one per-state file only:

```bash
cp config/states/_template.json config/states/florida.json
# Edit florida.json (see the required fields below)
aws s3 cp config/states/florida.json s3://${BUCKET}/config/states/florida.json
python scripts/seed_calibration_data.py --state florida --seasons 3
```

The system auto-discovers active states by listing the `config/states/` prefix on the next execution — no redeploy required.

### Required fields when editing the state file

The template ships with placeholders; three fields are load-bearing and easy to miss:

- **`enabled`** — the template ships `"enabled": false`. You **must** set it to `true`, or `list_active_states()` silently skips the file and nothing happens.
- **`cdc_geography_name`** — must match the CDC NSSP `geography` string for the state **exactly** (e.g. `"Florida"`). This is the key used to fetch the state-level surveillance feed, and it is the **reliable floor** for the county → HSA → state fallback that supplies per-county alert context. A typo here means rural counties with no CDC data of their own get no surveillance context at all.
- **`sentinel_metros[].county_fips` and `subscribing_counties[].county_fips`** — use real 5-digit FIPS. These lists don't just drive delivery; they now also select which counties are pulled from the county-level NSSP feed (`rdmq-nq56`). A county not listed here (or with a bad FIPS) gets no measured county signal and no county→HSA map, which disables its HSA-level fallback (it can still reach the state floor).

You do **not** need to create any county, NSSP, or HSA config file. County selection is derived from the state file above, `config/data_sources/cdc_nssp_county.json` is a global file already deployed, and county→HSA mappings self-populate from the CDC feed at ingestion time.

> **Cache note:** "auto-discovers on next execution" applies once warm Lambdas pick up the new config. The config loader caches per instance, so a very recently warmed Lambda may serve stale config briefly — see the cache-bust guidance in [Troubleshooting](#troubleshooting) if a new state doesn't appear on the next run.

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
# Set your verified SES email to receive the test alert (optional but recommended)
export HEALTHSIGNALS_TEST_EMAIL=your-verified@email.com

chmod +x scripts/test_end_to_end.sh
./scripts/test_end_to_end.sh
```

**What the script does:**

1. Checks prerequisites (AWS CLI, Lambda exists, config in S3, Delphi data)
2. Temporarily lowers the flu threshold to 0.01% AND disables `require_rising_trend`
3. Clears alert state for the test season and invalidates Lambda caches
4. Creates a temporary test subscription for Erath County (if `HEALTHSIGNALS_TEST_EMAIL` is set)
5. Invokes the pipeline coordinator
6. Polls Step Functions until completion
7. Reports PASS/FAIL with generated alert preview
8. Restores original config and removes the test subscription

**Duration:** ~90 seconds. **Cost:** ~$0.10 in Bedrock tokens.

**Environment variable:**

- `HEALTHSIGNALS_TEST_EMAIL` — set to a verified SES email address to receive the alert. If not set, the pipeline runs end-to-end but delivery is skipped (no subscriber exists for the test county).

**If the test reports "No alerts triggered":**

- Verify Delphi data exists: `aws s3 ls s3://${BUCKET}/raw/delphi/ --recursive | tail -5`
- Check Lambda caches were invalidated (script does this automatically)
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
