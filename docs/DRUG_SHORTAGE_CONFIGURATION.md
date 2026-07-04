# Drug Shortage Intelligence Module — Configuration Guide

## Overview

The Drug Shortage Intelligence Module extends Amazon HealthSignals with pharmaceutical supply chain monitoring using the openFDA Drug Shortages API. It provides **reactive monitoring** (not predictive) to help rural health facilities prepare for medication availability challenges during disease outbreaks.

**Key capabilities:**

1. **Standalone Shortage Alerts** — Pharmacists receive notifications when monitored therapeutic categories experience NEW or WORSENING shortages
2. **Combined Disease+Shortage Signals** — Disease outbreak alerts enriched with drug availability context when relevant medications are in shortage
3. **Subscription Filtering** — Facilities subscribe to specific therapeutic categories (antivirals, antibiotics, respiratory medications)

**Important:** All outputs include "FOR PHARMACIST REVIEW ONLY" disclaimers. The system never provides specific drug substitution recommendations.

---

## Configuration Files

| File                       | Location                                                         | Purpose                                                  |
| -------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------- |
| **Data Source Config**     | `config/data_sources/openfda_shortages.json`                     | API endpoint, rate limits, retry settings, S3 storage    |
| **Therapeutic Categories** | `config/shortage_monitoring/therapeutic_categories.json`         | Monitored drug categories, FDA mappings, priority levels |
| **Category Template**      | `config/shortage_monitoring/_template_therapeutic_category.json` | Empty template for adding new categories                 |

All configs are loaded from S3 at Lambda cold start using the existing `config_loader` shared utility.

---

## Schema: openfda_shortages.json

Defines the openFDA Drug Shortages API connection parameters. Follows the same pattern as other data source configs (`config/data_sources/delphi.json`, etc.).

```json
{
  "source_name": "openfda_drug_shortages",
  "display_name": "FDA Drug Shortages Database",
  "description": "Current and resolved drug shortage data from FDA openFDA API",
  "enabled": true,
  "priority": "supplemental",

  "api": {
    "base_url": "https://api.fda.gov/drug/shortages.json",
    "auth_type": "none",
    "rate_limit_per_hour": 240,
    "timeout_seconds": 30,
    "retry_max_attempts": 3,
    "retry_backoff_seconds": 5,
    "pagination": {
      "enabled": true,
      "limit_param": "limit",
      "default_limit": 1000
    }
  },

  "s3_storage": {
    "prefix_pattern": "raw/openfda-shortages/{year}/W{week}/shortages_{timestamp}.json"
  },

  "notes": {
    "sla": "Public API with no SLA — FDA may change schema without notice",
    "update_frequency": "Weekly (Monday 6 AM UTC via EventBridge)",
    "coverage": "All FDA-tracked drug shortages (~1,638 active records as of 2024)",
    "risk": "API schema may change without versioning. Monitor for 404/schema errors.",
    "validated": "2025-07-01"
  }
}
```

### Field Descriptions

| Field                          | Type    | Required | Description                                                 |
| ------------------------------ | ------- | -------- | ----------------------------------------------------------- |
| `source_name`                  | string  | Yes      | Internal identifier (snake_case)                            |
| `display_name`                 | string  | Yes      | Human-readable name for dashboards and logs                 |
| `description`                  | string  | Yes      | Brief description of data source                            |
| `enabled`                      | boolean | Yes      | Set `false` to disable fetching without removing config     |
| `priority`                     | string  | Yes      | `"supplemental"` — shortage data supplements disease alerts |
| `api.base_url`                 | string  | Yes      | openFDA endpoint URL. Validation fails if missing           |
| `api.auth_type`                | string  | Yes      | Always `"none"` — openFDA requires no authentication        |
| `api.rate_limit_per_hour`      | integer | Yes      | Max requests per hour (240 per openFDA docs)                |
| `api.timeout_seconds`          | integer | Yes      | Request timeout. Validation fails if missing                |
| `api.retry_max_attempts`       | integer | Yes      | Retries before sending to DLQ                               |
| `api.retry_backoff_seconds`    | integer | Yes      | Initial backoff delay (doubles each retry)                  |
| `api.pagination.enabled`       | boolean | Yes      | Whether API supports pagination                             |
| `api.pagination.limit_param`   | string  | Yes      | Query parameter name for page size                          |
| `api.pagination.default_limit` | integer | Yes      | Records per request (max 1000 for openFDA)                  |
| `s3_storage.prefix_pattern`    | string  | Yes      | S3 key template with `{year}`, `{week}`, `{timestamp}`      |
| `notes`                        | object  | No       | Documentation-only metadata (not used at runtime)           |

---

## Schema: therapeutic_categories.json

Defines which drug categories the system monitors, how to classify FDA products into categories, and how categories relate to diseases for combined signal generation.

```json
{
  "version": "1.0",
  "description": "Therapeutic categories monitored for drug shortage alerts.",

  "categories": [
    {
      "category_key": "antivirals",
      "display_name": "Antivirals",
      "description": "Antiviral medications used to treat influenza, COVID-19, and other viral infections",
      "priority_level": "HIGH",
      "relevant_diseases": ["influenza", "covid"],
      "fda_classification_mapping": [
        "*oseltamivir*",
        "*tamiflu*",
        "*zanamivir*",
        "*antiviral*"
      ]
    }
  ],

  "schema": {
    "required_fields": [
      "category_key",
      "display_name",
      "description",
      "priority_level",
      "relevant_diseases",
      "fda_classification_mapping"
    ],
    "valid_priority_levels": ["HIGH", "MEDIUM", "LOW"],
    "category_key_pattern": "^[a-z][a-z0-9_]*$"
  }
}
```

### Category Entry Fields

| Field                        | Type   | Required | Description                                                                                     |
| ---------------------------- | ------ | -------- | ----------------------------------------------------------------------------------------------- |
| `category_key`               | string | Yes      | Unique identifier. Lowercase alphanumeric + underscores. Must match pattern `^[a-z][a-z0-9_]*$` |
| `display_name`               | string | Yes      | Human-readable name shown in alerts and subscription UI                                         |
| `description`                | string | Yes      | Explanation of what this category covers                                                        |
| `priority_level`             | string | Yes      | Alert routing priority: `HIGH`, `MEDIUM`, or `LOW`                                              |
| `relevant_diseases`          | array  | Yes      | Disease keys from `config/diseases/` that this category relates to                              |
| `fda_classification_mapping` | array  | Yes      | Glob patterns (fnmatch-style) for matching openFDA product names                                |

### Schema Validation Block

| Field                          | Type   | Description                                         |
| ------------------------------ | ------ | --------------------------------------------------- |
| `schema.required_fields`       | array  | Fields that must be present in every category entry |
| `schema.valid_priority_levels` | array  | Allowed values for `priority_level`                 |
| `schema.category_key_pattern`  | string | Regex pattern that `category_key` must match        |

---

## Examples

### Adding a New Therapeutic Category (e.g., "Cardiovascular")

1. Open `config/shortage_monitoring/therapeutic_categories.json`
2. Add a new entry to the `categories` array:

```json
{
  "category_key": "cardiovascular",
  "display_name": "Cardiovascular Medications",
  "description": "Heart and blood pressure medications critical for chronic disease management during surge events",
  "priority_level": "MEDIUM",
  "relevant_diseases": ["covid"],
  "fda_classification_mapping": [
    "*lisinopril*",
    "*amlodipine*",
    "*metoprolol*",
    "*atorvastatin*",
    "*warfarin*",
    "*coumadin*",
    "*heparin*",
    "*nitroglycerin*",
    "*antihypertensive*",
    "*anticoagulant*"
  ]
}
```

3. Upload to S3:

```bash
aws s3 cp config/shortage_monitoring/therapeutic_categories.json \
  s3://${CONFIG_BUCKET}/config/shortage_monitoring/therapeutic_categories.json
```

4. Next weekly run automatically picks up the new category. No code changes required.

### Mapping FDA Classifications to Internal Categories

The `fda_classification_mapping` field uses **fnmatch-style glob patterns** (case-insensitive) to match openFDA `productName` or `genericName` fields against your categories.

**Pattern rules:**

| Pattern         | Matches                                | Example                           |
| --------------- | -------------------------------------- | --------------------------------- |
| `*drug*`        | Any string containing "drug"           | "Antiviral drug tablets"          |
| `*amoxicillin*` | Product names containing "amoxicillin" | "Amoxicillin Capsules, USP 500mg" |
| `drug*`         | Strings starting with "drug"           | Not recommended (too narrow)      |
| `*drug`         | Strings ending with "drug"             | Not recommended (too narrow)      |

**Best practices:**

- Always wrap patterns with `*` on both sides (e.g., `*oseltamivir*`) to match anywhere in the product name
- Include both brand and generic names (e.g., `*tamiflu*` and `*oseltamivir*`)
- Include broad category terms as a catch-all (e.g., `*antiviral*`, `*antibiotic*`)
- Patterns are matched case-insensitively
- A product matching multiple categories is assigned to the **first matching category** in the config file

**Matching logic:**

```python
# Simplified matching (actual implementation in parser.py)
import fnmatch

for category in categories:
    for pattern in category["fda_classification_mapping"]:
        if fnmatch.fnmatch(product_name.lower(), pattern.lower()):
            return category["category_key"]

return "Uncategorized"  # Excluded from alert generation
```

### Configuring Shortage-to-Disease Relationships

The `relevant_diseases` array links therapeutic categories to disease outbreak monitoring. This enables **combined signals** — when a disease outbreak is detected AND relevant medications are in shortage, the system produces an enriched alert.

```json
{
  "category_key": "antibiotics",
  "relevant_diseases": ["influenza", "covid", "rsv"]
}
```

**How it works:**

1. Pipeline Coordinator detects an influenza outbreak in Houston
2. System checks: which therapeutic categories have `"influenza"` in their `relevant_diseases`?
3. Matches: `antivirals` and `antibiotics`
4. System queries shortage state for those categories with status NEW or WORSENING
5. If matches found → alert_type = "combined" (disease + shortage context)
6. If no matches → alert_type = "disease_outbreak" (standard alert, no shortage enrichment)

**Valid disease keys** must exist in `config/diseases/` directory:

| Disease Key | Config File                      |
| ----------- | -------------------------------- |
| `influenza` | `config/diseases/influenza.json` |
| `covid`     | `config/diseases/covid.json`     |
| `rsv`       | `config/diseases/rsv.json`       |

---

## Field Reference

### `priority_level`

Controls alert routing severity and delivery urgency.

| Value    | Behavior                                                                                                                                       | Use Case                                                                       |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `HIGH`   | Alerts generated immediately on detection. Delivered via all enabled channels (email + SMS). Appears in dashboard with red severity indicator. | Antivirals, antibiotics — medications directly needed during outbreak response |
| `MEDIUM` | Alerts generated on detection. Delivered via email only (SMS skipped unless explicitly enabled). Yellow severity indicator.                    | Respiratory medications — important but broader category                       |
| `LOW`    | Alerts generated on detection but batched into weekly digest. Green severity indicator.                                                        | Supplemental categories — good to know but not urgent                          |

### `relevant_diseases`

Array of `disease_key` string values that creates the link between drug categories and disease monitoring.

- Values must match existing disease config filenames (without `.json` extension) in `config/diseases/`
- Used by Pipeline Coordinator to determine if shortage context should enrich a disease outbreak alert
- An empty array `[]` means the category generates standalone shortage alerts only (no combined signals)
- A category can relate to multiple diseases

**Example:** Antibiotics relate to influenza, COVID, and RSV because secondary bacterial infections are common complications of all three respiratory diseases.

### `fda_classification_mapping`

Array of glob patterns (fnmatch-style) for classifying openFDA product names into therapeutic categories.

**Rules:**

- Patterns use `*` as wildcard (matches any number of characters)
- Patterns use `?` to match exactly one character
- Matching is case-insensitive
- The parser tests `productName` first, then `genericName` as fallback
- First matching category wins (order matters in the config file)
- Products matching no pattern are classified as `"Uncategorized"` and excluded from alerts
- Patterns should be specific enough to avoid false positives but broad enough to catch variations (e.g., different dosage forms, manufacturers)

---

## Migration Guide

How to add Drug Shortage monitoring to an existing HealthSignals deployment.

### Prerequisites

- HealthSignals v1.0+ deployed with all 7 CDK stacks operational
- S3 config bucket accessible
- DynamoDB tables provisioned (Prediction Stack)
- EventBridge weekly schedule active (Monday 6 AM UTC)

### Step 1: Deploy Configuration Files

```bash
# Upload data source config
aws s3 cp config/data_sources/openfda_shortages.json \
  s3://${CONFIG_BUCKET}/config/data_sources/openfda_shortages.json

# Upload therapeutic categories
aws s3 cp config/shortage_monitoring/therapeutic_categories.json \
  s3://${CONFIG_BUCKET}/config/shortage_monitoring/therapeutic_categories.json
```

### Step 2: Deploy CDK Stack Updates

```bash
cd cdk/

# Deploy infrastructure changes (DynamoDB tables, Lambda functions, SQS queues)
cdk deploy HealthSignals-Ingestion HealthSignals-Prediction \
  HealthSignals-Orchestration HealthSignals-Generation \
  HealthSignals-Subscription HealthSignals-Delivery \
  HealthSignals-Monitoring
```

This adds:

- `healthsignals-openfda-shortages-queue` (SQS)
- `healthsignals-drug-shortage-state` (DynamoDB)
- `healthsignals-shortage-alerts` (DynamoDB)
- openFDA fetcher Lambda
- Shortage change detector Lambda
- Extended Pipeline Coordinator with shortage routing
- Extended Step Functions with shortage alert branching
- GSI `therapeutic-category-lookup` on subscriptions table

### Step 3: Verify Configuration Loading

```bash
# Invoke fetcher with test event to verify config loads correctly
aws lambda invoke \
  --function-name healthsignals-openfda-shortage-fetcher \
  --payload '{"source": "manual_test"}' \
  --cli-binary-format raw-in-base64-out \
  response.json

cat response.json
# Expected: {"statusCode": 200, "records_fetched": ..., "s3_key": "raw/openfda-shortages/..."}
```

### Step 4: Enable Subscriptions

Existing subscribers can opt into shortage alerts via the Subscription API:

```bash
curl -X PUT https://${API_ENDPOINT}/preferences \
  -H "Content-Type: application/json" \
  -d '{
    "county_fips": "48143",
    "therapeutic_categories": ["antivirals", "antibiotics", "respiratory"]
  }'
```

### Step 5: Verify End-to-End

Wait for the next Monday 6 AM UTC run, or manually trigger:

```bash
# Send test message to SQS to trigger the pipeline
aws sqs send-message \
  --queue-url https://sqs.${AWS_REGION}.amazonaws.com/${ACCOUNT_ID}/healthsignals-openfda-shortages-queue \
  --message-body '{"source": "scheduled", "trigger": "weekly_monday"}'
```

Check CloudWatch dashboard → "Drug Shortage Intelligence" section for metrics.

---

## Troubleshooting

| Symptom                                                   | Cause                                                  | Resolution                                                                                       |
| --------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| "ConfigLoadError: missing required field base_url"        | `openfda_shortages.json` missing `api.base_url`        | Verify config file has complete `api` block with all required fields                             |
| "ConfigLoadError: missing required field timeout_seconds" | `openfda_shortages.json` missing `api.timeout_seconds` | Add `"timeout_seconds": 30` to the `api` block                                                   |
| "Invalid priority_level: CRITICAL"                        | Unsupported value in `priority_level`                  | Use only `HIGH`, `MEDIUM`, or `LOW`                                                              |
| "Invalid category_key: Anti-Virals"                       | Key doesn't match pattern `^[a-z][a-z0-9_]*$`          | Use lowercase + underscores only (e.g., `antivirals`)                                            |
| "Disease key 'flu' not found in config/diseases/"         | `relevant_diseases` references nonexistent disease     | Use exact disease_key from disease config filenames (e.g., `influenza` not `flu`)                |
| Products not matching any category                        | Glob patterns too specific                             | Add broader patterns (e.g., `*antiviral*`) and check case sensitivity                            |
| Duplicate alerts sent                                     | Idempotency record missing in shortage-alerts table    | Check DynamoDB `healthsignals-shortage-alerts` for existing records. Delete record to regenerate |
| Circuit breaker activated                                 | >20 NEW/WORSENING shortages in one week                | Review CloudWatch logs for legitimacy. If valid, manually clear circuit breaker and re-run       |
| "HTTP 404 from openFDA"                                   | API endpoint changed                                   | Check openFDA docs for updated endpoint. Update `api.base_url` in config                         |
| "HTTP 429 rate limit"                                     | Exceeding 240 requests/hour                            | Reduce `api.pagination.default_limit` or add delays between paginated requests                   |
| Subscription filtering not working                        | Missing GSI `therapeutic-category-lookup`              | Redeploy Subscription Stack: `cdk deploy HealthSignals-Subscription`                             |
| No shortage context in disease alerts                     | `relevant_diseases` array empty or mismatched          | Verify disease_key values match exactly between category config and disease configs              |
| Config not refreshing                                     | Lambda warm instance using cached config               | Invoke with `{"_refresh_config": true}` or wait for cold start (~15-45 min)                      |

---

## Phase 2 Roadmap

Future enhancements planned beyond the Phase 1 MVP:

| Enhancement                         | Description                                                                                                                                                                      | Status                  |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| **Predictive Modeling**             | ML model trained on historical shortage patterns to predict upcoming shortages 2-4 weeks in advance. Would use time-series analysis on openFDA data + seasonal disease patterns. | Planned                 |
| **Real-time Webhooks**              | Replace weekly polling with event-driven architecture. Subscribe to openFDA update notifications (if/when available) for immediate shortage detection.                           | Pending FDA API support |
| **Pharmacy System Integrations**    | Direct integrations with pharmacy management systems (e.g., McKesson, Cardinal Health) for inventory-aware alerts — only alert when a facility's actual stock is impacted.       | Research                |
| **Shortage Intelligence Dashboard** | Interactive web dashboard showing current shortage landscape, historical trends, geographic impact maps, and facility-specific risk scores.                                      | Design phase            |
| **Multi-state Correlation**         | Detect shortage patterns that correlate with disease spread across state boundaries for interstate preparedness coordination.                                                    | Planned                 |
| **Compounding Pharmacy Network**    | Integration with 503B outsourcing facilities to include alternative supply availability in shortage alerts.                                                                      | Research                |

---

_Last updated: 2025-07-01_
_Module version: 1.0 (Phase 1 MVP)_
