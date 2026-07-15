# Drug Shortage Module — Manual Testing Guide

## Prerequisites

- HealthSignals deployed with Drug Shortage module (all CDK stacks)
- Config files uploaded to S3 (`config/data_sources/openfda_shortages.json`, `config/shortage_monitoring/therapeutic_categories.json`)
- At least one subscription with `therapeutic_categories` configured
- AWS CLI configured with appropriate credentials

## Test 1: Trigger Standalone Shortage Alert

### Step 1: Invoke openFDA Fetcher

```bash
# Manually invoke the openFDA shortage fetcher Lambda
aws lambda invoke \
  --function-name healthsignals-openfda-shortage-fetcher \
  --payload '{"source": "manual_test"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

**Expected output:**

```json
{
  "statusCode": 200,
  "s3_key": "raw/openfda-shortages/2025/W27/shortages_20250702_060000.json",
  "records_fetched": 1638,
  "timestamp": "2025-07-02T06:00:00Z"
}
```

### Step 2: Verify S3 Object Written

```bash
aws s3 ls s3://${DATA_BUCKET}/raw/openfda-shortages/ --recursive | tail -5
```

### Step 3: Verify DynamoDB Records

```bash
# Check shortage-state table for new records
aws dynamodb scan \
  --table-name healthsignals-drug-shortage-state \
  --filter-expression "shortage_status = :new" \
  --expression-attribute-values '{":new":{"S":"NEW"}}' \
  --select COUNT
```

### Step 4: Check Step Functions Execution

```bash
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:stateMachine:healthsignals-alert-generation \
  --max-results 5 \
  --status-filter RUNNING
```

### Step 5: Verify Alert Delivery

```bash
# Check shortage-alerts table for SENT records
aws dynamodb scan \
  --table-name healthsignals-shortage-alerts \
  --filter-expression "alert_generated = :sent" \
  --expression-attribute-values '{":sent":{"S":"SENT"}}' \
  --max-items 5
```

---

## Test 2: Subscribe to Therapeutic Categories

### Subscribe via API

```bash
# Subscribe to Antivirals and Antibiotics categories
curl -X PUT https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/prod/preferences \
  -H "Content-Type: application/json" \
  -d '{
    "county_fips": "48143",
    "subscription_id": "YOUR_SUBSCRIPTION_ID",
    "updates": {
      "therapeutic_categories": ["antivirals", "antibiotics", "respiratory"]
    }
  }'
```

**Expected response:**

```json
{
  "message": "Preferences updated successfully.",
  "subscription_id": "YOUR_SUBSCRIPTION_ID",
  "updated_fields": ["therapeutic_categories"],
  "current_status": "active"
}
```

### Verify Subscription Status

```bash
curl https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/prod/status?county_fips=48143
```

**Expected:** Response includes `"therapeutic_categories": ["antivirals", "antibiotics", "respiratory"]`

### Test Invalid Category

```bash
curl -X PUT https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/prod/preferences \
  -H "Content-Type: application/json" \
  -d '{
    "county_fips": "48143",
    "subscription_id": "YOUR_SUBSCRIPTION_ID",
    "updates": {
      "therapeutic_categories": ["antivirals", "invalid_category"]
    }
  }'
```

**Expected:** HTTP 400 with `{"error": "Invalid therapeutic category: invalid_category"}`

---

## Test 3: Combined Disease + Shortage Signal

### Step 1: Seed shortage-state with relevant medication

```bash
# Add a test antiviral shortage record for current week
CURRENT_WEEK=$(date +%G-W%V)

aws dynamodb put-item \
  --table-name healthsignals-drug-shortage-state \
  --item "{
    \"product_id\": {\"S\": \"TEST-OSELTAMIVIR-001\"},
    \"week_timestamp\": {\"S\": \"${CURRENT_WEEK}\"},
    \"product_name\": {\"S\": \"Oseltamivir Capsules 75mg\"},
    \"therapeutic_category\": {\"S\": \"antivirals\"},
    \"supply_status\": {\"S\": \"DISCONTINUED\"},
    \"reason_for_shortage\": {\"S\": \"Manufacturing delay\"},
    \"shortage_status\": {\"S\": \"NEW\"},
    \"created_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"},
    \"ttl\": {\"N\": \"$(($(date +%s) + 31449600))\"}
  }"
```

### Step 2: Trigger disease outbreak detection

Follow existing disease outbreak testing procedure to trigger a Delphi data fetch that crosses threshold.

### Step 3: Verify combined alert payload

```bash
# Check CloudWatch Logs for pipeline_coordinator
aws logs filter-log-events \
  --log-group-name /aws/lambda/healthsignals-pipeline-coordinator \
  --filter-pattern '"shortage_context"' \
  --limit 5
```

**Expected:** Log entries showing `shortage_context` populated with the test oseltamivir record.

### Step 4: Cleanup test record

```bash
aws dynamodb delete-item \
  --table-name healthsignals-drug-shortage-state \
  --key "{
    \"product_id\": {\"S\": \"TEST-OSELTAMIVIR-001\"},
    \"week_timestamp\": {\"S\": \"${CURRENT_WEEK}\"}
  }"
```

---

## Test 4: Circuit Breaker Verification

### Trigger circuit breaker

```bash
# Upload test data with >20 NEW shortage records to S3
# (Create a file with 25 fake shortage records, all in monitored categories)
python -c "
import json
records = []
for i in range(25):
    records.append({
        'product_id': f'TEST-CB-{i:03d}',
        'product_name': f'Test Amoxicillin Variant {i}',
        'supply_status': 'DISCONTINUED',
        'reason_for_shortage': 'Test circuit breaker',
        'therapeutic_category': 'antibiotics',
        'week_timestamp': '$(date +%G-W%V)',
    })
print(json.dumps(records))
" > /tmp/circuit_breaker_test.json

aws s3 cp /tmp/circuit_breaker_test.json \
  s3://${DATA_BUCKET}/raw/openfda-shortages/$(date +%Y)/W$(date +%V)/shortages_test_cb.json
```

### Verify circuit breaker activated

```bash
# Check CloudWatch metric
aws cloudwatch get-metric-data \
  --metric-data-queries '[{"Id":"cb","MetricStat":{"Metric":{"Namespace":"HealthSignals/DrugShortages","MetricName":"shortage_circuit_breaker_activations"},"Period":300,"Stat":"Sum"}}]' \
  --start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S)
```

**Expected:** Value of 1 in the metric.

---

## Test 5: Validate Configuration

```bash
# Run the configuration validation script
python scripts/validate_shortage_config.py
```

**Expected:** `OK: All configuration files are valid`

---

## CloudWatch Monitoring

### Key Metrics to Check

| Metric                                 | Namespace                   | What to verify        |
| -------------------------------------- | --------------------------- | --------------------- |
| `records_fetched_count`                | HealthSignals/DrugShortages | >0 after fetcher run  |
| `api_success_rate`                     | HealthSignals/DrugShortages | 1.0 (success)         |
| `shortage_changes_detected_count`      | HealthSignals/DrugShortages | Expected change count |
| `shortage_alerts_generated_count`      | HealthSignals/DrugShortages | Alerts triggered      |
| `shortage_circuit_breaker_activations` | HealthSignals/DrugShortages | 0 in normal ops       |

### CloudWatch Logs

```bash
# openFDA Fetcher logs
aws logs tail /aws/lambda/healthsignals-openfda-shortage-fetcher --follow

# Shortage Change Detector logs
aws logs tail /aws/lambda/healthsignals-shortage-change-detector --follow

# Pipeline Coordinator logs (shortage routing)
aws logs filter-log-events \
  --log-group-name /aws/lambda/healthsignals-pipeline-coordinator \
  --filter-pattern '"openfda-shortages"'
```

---

_Last updated: 2025-07-02_
