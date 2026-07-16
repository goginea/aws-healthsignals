# Drug Shortage Intelligence Module — Configuration Guide

## Overview

The Drug Shortage Intelligence Module is an optional plugin that extends HealthSignals with pharmaceutical supply chain monitoring. It operates as a self-contained add-on with its own CDK stack, Lambdas, Step Functions state machine, DynamoDB tables, and CloudWatch monitoring.

**Key capabilities:**

1. **Standalone Shortage Alerts** — Pharmacists receive notifications when monitored therapeutic categories experience NEW or WORSENING shortages
2. **Combined Disease+Shortage Signals** — Disease outbreak alerts enriched with drug availability context when relevant medications are in shortage
3. **Category-based Subscriptions** — Facilities subscribe to specific categories (antivirals, antibiotics, respiratory)

All outputs include "FOR PHARMACIST REVIEW ONLY" disclaimers. The system does not provide specific drug substitution recommendations.

---

## Architecture

The module is fully decoupled from core HealthSignals:

```
Core Pipeline Coordinator
    │
    ├── Detects disease threshold → starts core SFN (disease_outbreak)
    └── Emits EventBridge: "healthsignals.disease.threshold_crossed"
                                    │
                                    ▼
              Shortage Enrichment Lambda (plugin)
                    ├── Queries shortage-state DynamoDB
                    └── If shortages found → starts shortage SFN (combined)

S3: raw/openfda-shortages/*.json
    │
    └── S3 notification → Shortage Change Detector (plugin)
              └── Classifies changes → starts shortage SFN (shortage)
```

**Trigger model:**

- openFDA fetcher runs weekly via EventBridge schedule (Monday 6 AM UTC)
- Change detector is triggered directly by S3 event notification
- Enrichment Lambda subscribes to EventBridge events from the core pipeline coordinator

---

## Enabling the Module

In `cdk/cdk.json`:

```json
{
  "context": {
    "enable_drug_shortage": true
  }
}
```

Then deploy:

```bash
npx aws-cdk deploy --all
```

This creates the `HealthSignals-DrugShortage` stack and updates the Delivery and Subscription stacks with plugin configuration.

---

## Configuration Files

| File                                                     | Purpose                                                              |
| -------------------------------------------------------- | -------------------------------------------------------------------- |
| `config/data_sources/openfda_shortages.json`             | API endpoint, pagination, retry settings                             |
| `config/shortage_monitoring/therapeutic_categories.json` | Monitored drug categories and disease mappings                       |
| `config/alert_categories.json`                           | Shared subscriber opt-in categories (used by update_preferences API) |

---

## Schema: openfda_shortages.json

```json
{
  "source_name": "openfda_drug_shortages",
  "enabled": true,
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
  }
}
```

---

## Schema: therapeutic_categories.json

```json
{
  "categories": [
    {
      "category_key": "antivirals",
      "display_name": "Antivirals",
      "priority_level": "HIGH",
      "relevant_diseases": ["influenza", "covid"],
      "fda_classification_mapping": [
        "*oseltamivir*",
        "*tamiflu*",
        "*antiviral*"
      ]
    }
  ]
}
```

| Field                        | Description                                  |
| ---------------------------- | -------------------------------------------- |
| `category_key`               | Unique identifier (lowercase, underscores)   |
| `display_name`               | Human-readable name for alerts               |
| `priority_level`             | `HIGH`, `MEDIUM`, or `LOW`                   |
| `relevant_diseases`          | Disease keys that trigger combined alerts    |
| `fda_classification_mapping` | Glob patterns to match openFDA product names |

---

## Adding a Therapeutic Category

1. Edit `config/shortage_monitoring/therapeutic_categories.json`
2. Add entry to `categories` array
3. Upload to S3:
   ```bash
   aws s3 cp config/shortage_monitoring/therapeutic_categories.json \
     s3://${CONFIG_BUCKET}/config/shortage_monitoring/therapeutic_categories.json
   ```
4. Next weekly run picks up the new category automatically

---

## CDK Resources (HealthSignals-DrugShortage Stack)

| Resource                                  | Type           | Purpose                                                 |
| ----------------------------------------- | -------------- | ------------------------------------------------------- |
| `healthsignals-drug-shortage-state`       | DynamoDB       | Historical shortage state with therapeutic-category GSI |
| `healthsignals-shortage-alerts`           | DynamoDB       | Alert idempotency tracking                              |
| `healthsignals-openfda-shortage-fetcher`  | Lambda         | Weekly openFDA API polling                              |
| `healthsignals-shortage-change-detector`  | Lambda         | Change classification (NEW/WORSENING/RESOLVED)          |
| `healthsignals-shortage-enrichment`       | Lambda         | Combined signal enrichment via EventBridge              |
| `healthsignals-shortage-alert-generation` | Step Functions | Bedrock brief generation for shortage/combined alerts   |
| Weekly EventBridge rule                   | Schedule       | Triggers fetcher every Monday                           |
| Disease threshold EventBridge rule        | Event pattern  | Subscribes to core disease detections                   |
| S3 event notification                     | Trigger        | Fires change detector on new data                       |
| 3 CloudWatch alarms                       | Monitoring     | API health, circuit breaker, DLQ depth                  |
| CloudWatch dashboard                      | Monitoring     | Shortage-specific metrics                               |

---

## Subscription Filtering

Subscribers opt into shortage alerts by category:

```bash
curl -X PUT https://${API}/preferences \
  -d '{"county_fips": "48143", "subscription_id": "...", "updates": {"alert_categories": ["antivirals"]}}'
```

The module queries the `alert-category-lookup` GSI on the subscriptions table to find matching subscribers.

---

## Circuit Breaker

If >20 NEW/WORSENING shortages are detected in one week, the circuit breaker activates:

- No alerts are generated
- CloudWatch alarm fires
- Manual review required before next run

---

## Troubleshooting

| Symptom                          | Cause                                   | Fix                                                |
| -------------------------------- | --------------------------------------- | -------------------------------------------------- |
| No alerts generated              | Circuit breaker activated               | Check CloudWatch alarm, review data legitimacy     |
| `HTTP 404 from openFDA`          | API endpoint changed                    | Update `api.base_url` in config                    |
| Products not matching categories | Glob patterns too specific              | Add broader patterns                               |
| No combined alerts               | `relevant_diseases` empty or mismatched | Verify disease keys match disease config filenames |
| Config not refreshing            | Lambda warm cache                       | Force cold start                                   |

---

## Phase 2 Roadmap

| Enhancement                  | Description                                                 | Status                  |
| ---------------------------- | ----------------------------------------------------------- | ----------------------- |
| Predictive modeling          | ML on historical shortage patterns for 2-4 week forecasting | Planned                 |
| Real-time webhooks           | Replace polling with event-driven FDA notifications         | Pending FDA API support |
| Pharmacy system integrations | Inventory-aware alerts via McKesson/Cardinal Health         | Research                |
| Multi-state correlation      | Cross-border shortage pattern detection                     | Planned                 |
