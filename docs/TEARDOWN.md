# Teardown Guide — Amazon HealthSignals

Complete removal of all HealthSignals resources from your AWS account.

---

## Quick Teardown (CDK)

```bash
cd aws-healthsignals/cdk
npx aws-cdk destroy --all --force
```

This removes all CloudFormation stacks (7 core + any enabled plugin stacks) and their resources.

If the Drug Shortage module was enabled, this also removes:

- `HealthSignals-DrugShortage` stack (DynamoDB tables, Lambdas, Step Functions, SQS, alarms, dashboard)

---

## Resources NOT Deleted by CDK

The following resources have `RemovalPolicy.RETAIN` and must be deleted manually:

### 1. S3 Data Bucket

```bash
# Delete all object versions (required for versioned buckets)
aws s3api delete-objects --bucket healthsignals-data-ACCOUNT_ID-REGION \
  --delete "$(aws s3api list-object-versions --bucket healthsignals-data-ACCOUNT_ID-REGION \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)"

# Delete all delete markers
aws s3api delete-objects --bucket healthsignals-data-ACCOUNT_ID-REGION \
  --delete "$(aws s3api list-object-versions --bucket healthsignals-data-ACCOUNT_ID-REGION \
  --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json)"

# Delete the bucket
aws s3 rb s3://healthsignals-data-ACCOUNT_ID-REGION
```

> **Note:** `aws s3 rb --force` does NOT work on versioned buckets. You must delete all versions and delete markers first using the commands above.

> ⚠️ This permanently deletes all surveillance data, config files, and Knowledge Base documents. Export any data you need before running this.

### 2. Secrets Manager Secret

```bash
aws secretsmanager delete-secret \
  --secret-id healthsignals/token-signing-key \
  --force-delete-without-recovery
```

### 3. DynamoDB Tables (if RETAIN policy was set)

The CDK stacks use `RemovalPolicy.RETAIN` on critical tables. If they weren't deleted with the stack:

```bash
aws dynamodb delete-table --table-name healthsignals-alert-state
aws dynamodb delete-table --table-name healthsignals-calibration
aws dynamodb delete-table --table-name healthsignals-county-configs
aws dynamodb delete-table --table-name healthsignals-feedback
aws dynamodb delete-table --table-name healthsignals-pipeline-runs
aws dynamodb delete-table --table-name healthsignals-subscriptions

# Drug Shortage module tables (if enabled)
aws dynamodb delete-table --table-name healthsignals-drug-shortage-state 2>/dev/null
aws dynamodb delete-table --table-name healthsignals-shortage-alerts 2>/dev/null
```

### 4. Bedrock Knowledge Bases (if created in console)

If you created Bedrock Knowledge Bases manually:

1. Go to **Amazon Bedrock** → **Knowledge Bases** in the console
2. Delete each HealthSignals KB (CDC Guidelines, Communication Templates)
3. Delete the associated S3 data source and IAM role

### 5. CloudWatch Log Groups

Lambda log groups persist after stack deletion:

```bash
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-delphi-fetcher
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-cdc-wastewater-fetcher
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-cdc-respiratory-fetcher
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-leader-detection
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-geographic-affinity
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-timing-estimation
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-pipeline-coordinator
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-alert-dispatcher
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-feedback-recalibrator
aws logs delete-log-group --log-group-name /aws/states/healthsignals-alert-generation

# Drug Shortage module (if enabled)
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-openfda-shortage-fetcher 2>/dev/null
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-shortage-change-detector 2>/dev/null
aws logs delete-log-group --log-group-name /aws/lambda/healthsignals-shortage-enrichment 2>/dev/null
aws logs delete-log-group --log-group-name /aws/states/healthsignals-shortage-alert-generation 2>/dev/null
```

Or delete all at once:

```bash
# Lowercase prefix (Lambdas with explicit function_name)
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/healthsignals \
  --query 'logGroups[].logGroupName' --output text | \
  xargs -I{} aws logs delete-log-group --log-group-name {}

# PascalCase prefix (CDK auto-generated Lambda names from stack name)
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/HealthSignals \
  --query 'logGroups[].logGroupName' --output text | \
  xargs -I{} aws logs delete-log-group --log-group-name {}
```

> **Why two prefixes?** Lambdas with an explicit `function_name` in CDK (e.g., `healthsignals-alert-dispatcher`) get lowercase log groups. Lambdas without an explicit name (subscription functions, CDK internal handlers) get names auto-generated from the stack name prefix (`HealthSignals-*`).

### 6. Manually Added IAM Policies (if any)

If you added inline policies during deployment troubleshooting:

```bash
# These will fail gracefully if roles were already deleted with stacks
aws iam delete-role-policy --role-name <ROLE_NAME> --policy-name S3ConfigRead 2>/dev/null
aws iam delete-role-policy --role-name <ROLE_NAME> --policy-name BedrockInferenceProfileAccess 2>/dev/null
aws iam delete-role-policy --role-name <ROLE_NAME> --policy-name LambdaInvokeDispatcher 2>/dev/null
```

### 7. SES Verified Email Identity (optional)

If you verified an email address for alert delivery:

```bash
aws ses delete-identity --identity alerts@healthsignals.example.com
```

---

## CDK Bootstrap (Optional)

The CDK bootstrap stack (`CDKToolkit`) is shared across all CDK projects in your account. Only remove it if you don't plan to use CDK for anything else:

```bash
# Only if no other CDK stacks exist in this account/region
aws cloudformation delete-stack --stack-name CDKToolkit
aws s3 rm s3://cdk-hnb659fds-assets-ACCOUNT_ID-REGION --recursive
aws s3 rb s3://cdk-hnb659fds-assets-ACCOUNT_ID-REGION
```

---

## Verification

After teardown, verify nothing remains:

```bash
# Check for any remaining stacks
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?starts_with(StackName,'HealthSignals')].StackName"

# Check for any remaining Lambda functions
aws lambda list-functions \
  --query "Functions[?starts_with(FunctionName,'healthsignals')].FunctionName"

# Check for remaining DynamoDB tables
aws dynamodb list-tables \
  --query "TableNames[?starts_with(@,'healthsignals')]"

# Check for remaining S3 buckets
aws s3 ls | grep healthsignals
```

All commands should return empty results.

---

## Cost After Teardown

Once all resources are removed:

- **Ongoing cost: $0.00/month**
- No reserved capacity, no committed spend, no minimum fees
- CloudWatch log storage for retained logs is negligible (~$0.03/GB/month)

---

## Data Export (Before Teardown)

If you want to preserve data before removal:

```bash
# Export all S3 data locally
aws s3 sync s3://healthsignals-data-ACCOUNT_ID-REGION ./healthsignals-backup/

# Export DynamoDB tables
aws dynamodb scan --table-name healthsignals-calibration --output json > calibration-backup.json
aws dynamodb scan --table-name healthsignals-subscriptions --output json > subscriptions-backup.json
aws dynamodb scan --table-name healthsignals-pipeline-runs --output json > pipeline-runs-backup.json
```

---

_Total teardown time: ~5 minutes (CDK destroy) + ~5 minutes (manual cleanup)_
