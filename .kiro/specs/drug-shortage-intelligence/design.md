# Design Document: Drug Shortage Intelligence Module

## Overview

The Drug Shortage Intelligence Module extends Amazon HealthSignals to monitor pharmaceutical supply chain disruptions using the openFDA Drug Shortages API. This Phase 1 MVP provides **reactive monitoring** (not predictive analytics) to help rural health facilities prepare for medication availability challenges during disease outbreaks.

The module seamlessly integrates with the existing HealthSignals architecture by:

- Following the config-driven design pattern (zero code changes for scaling)
- Using the same weekly polling schedule (Monday 6 AM UTC via EventBridge)
- Leveraging existing infrastructure (S3 data lake, DynamoDB, Step Functions, Subscription API)
- Maintaining the deterministic workflow approach (not autonomous agents)

**Key Capabilities:**

1. **Standalone Shortage Alerts** — Pharmacists receive notifications when monitored therapeutic categories experience NEW or WORSENING shortages
2. **Combined Disease+Shortage Signals** — Disease outbreak alerts are enriched with drug availability context when relevant medications are in shortage
3. **Subscription Filtering** — Facilities subscribe to specific therapeutic categories (antivirals, antibiotics, respiratory medications)

**Important Disclaimers:**

- All outputs include "FOR PHARMACIST REVIEW ONLY" disclaimers
- The system never provides specific drug substitution recommendations (violates guardrails)
- This is monitoring-only; no predictive modeling of future shortages

## Architecture

### System Integration Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    EXISTING HEALTHSIGNALS ARCHITECTURE                     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  EventBridge (Mon 6AM) ──┬──▶ Delphi SQS ──▶ Delphi Fetcher ──▶ S3     │
│                          ├──▶ CDC NWSS SQS ──▶ CDC Fetcher ───▶ S3     │
│                          ├──▶ CDC Resp SQS ──▶ CDC Fetcher ───▶ S3     │
│                          │                                               │
│                          │  [NEW] Drug Shortage Module                   │
│                          └──▶ openFDA SQS ──▶ openFDA Fetcher ──▶ S3    │
│                                                ↓                          │
│                                   S3 PutObject Event                     │
│                                                ↓                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │         EXTENDED Pipeline Coordinator (Orchestration Lambda)       │  │
│  ├───────────────────────────────────────────────────────────────────┤  │
│  │  Disease Outbreak Path:                                           │  │
│  │    Leader Detection ──▶ Geographic Affinity ──▶ Timing Est.      │  │
│  │        ↓                                                           │  │
│  │    Check Shortage Context (NEW)                                   │  │
│  │        ↓                                                           │  │
│  │    Enrich if relevant meds in shortage                            │  │
│  │                                                                    │  │
│  │  [NEW] Shortage Monitoring Path:                                  │  │
│  │    Shortage Change Detector ──▶ Filter by Therapeutic Category   │  │
│  │        ↓                                                           │  │
│  │    Classify: NEW/WORSENING/RESOLVED/UNCHANGED                     │  │
│  └────────────────────────────────┬──────────────────────────────────┘  │
│                                   ↓                                      │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │       EXTENDED Step Functions Alert Generation Workflow           │  │
│  ├───────────────────────────────────────────────────────────────────┤  │
│  │                                                                    │  │
│  │  Branch on alert_type: disease_outbreak | shortage | combined     │  │
│  │                                                                    │  │
│  │  Disease Outbreak (existing):                                     │  │
│  │    Situation Brief ──▶ Severity ──▶ Checklist ──▶ Comms Drafting │  │
│  │                                                                    │  │
│  │  [NEW] Shortage Alert:                                            │  │
│  │    Shortage Situation Brief ──▶ Pharmacist Actions ──▶ Comms     │  │
│  │                                                                    │  │
│  │  [NEW] Combined Signal:                                           │  │
│  │    Disease Brief + Shortage Context ──▶ Severity ──▶ Checklist   │  │
│  │    with Medication Availability section                           │  │
│  └────────────────────────────────┬──────────────────────────────────┘  │
│                                   ↓                                      │
│                       EXTENDED Alert Dispatcher                          │
│                       (filters by therapeutic_categories in subscription)│
└─────────────────────────────────────────────────────────────────────────┘
```

### Architecture Decision: Integrated vs. Standalone

**Decision:** Integrate into existing 7 CDK stacks rather than creating a new shortage-specific stack.

**Rationale:**

1. **Consistency** — Follows established patterns (SQS+Lambda ingestion, DynamoDB state, Step Functions generation)
2. **Simplicity** — Reuses existing infrastructure (S3 bucket, EventBridge schedule, shared layers)
3. **Operational Efficiency** — Same monitoring dashboards, alarms, and deployment pipelines
4. **Cost Optimization** — No additional stack overhead or duplicated resources

**Integration Points by Stack:**

| Stack             | Extensions                                                                                                             |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Ingestion**     | +1 SQS queue (openFDA), +1 Lambda fetcher, +1 EventBridge target                                                       |
| **Prediction**    | +2 DynamoDB tables (shortage-state, shortage-alerts), +1 Lambda (change detector), +1 GSI (therapeutic-category-index) |
| **Generation**    | +2 Bedrock prompts (shortage briefs, combined alerts), +1 KB document collection (shortage guidance)                   |
| **Orchestration** | Extend Pipeline Coordinator with shortage detection + context enrichment logic                                         |
| **Subscription**  | +1 field (therapeutic_categories array), +1 GSI (therapeutic-category-lookup)                                          |
| **Delivery**      | Extend Alert Dispatcher with therapeutic category filtering                                                            |
| **Monitoring**    | +1 dashboard section (Drug Shortage Intelligence metrics)                                                              |

## Components and Interfaces

### 1. OpenFDA Shortage Fetcher Lambda

**Location:** `lambdas/ingestion/openfda_shortage_fetcher/`

**Responsibility:** Polls openFDA Drug Shortages API weekly and stores raw responses to S3.

**Interface:**

```python
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Triggered by: SQS message from EventBridge scheduler

    Input: event = {
        "source": "scheduled",
        "trigger": "weekly_monday"
    }

    Output: {
        "statusCode": 200,
        "records_fetched": 1638,
        "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
        "timestamp": "2024-01-15T06:05:12Z"
    }

    Failure modes:
    - HTTP 429 (rate limit): Exponential backoff 5s, 10s, 20s
    - HTTP 500/503: Retry with backoff
    - HTTP 404: Log error, skip retry (endpoint changed)
    - Timeout (>30s): Retry via SQS
    - After 3 retries: Send to DLQ, emit CloudWatch alarm
    """
    config = load_shortage_config()  # From S3: config/data_sources/openfda_shortages.json
    response = fetch_with_retry(config["api"]["base_url"], max_retries=3)
    normalized = parse_openfda_response(response)
    s3_key = store_to_s3(normalized, config["s3_storage"]["prefix_pattern"])
    emit_metrics(records_count=len(normalized))
    return {"statusCode": 200, "s3_key": s3_key}
```

**Dependencies:**

- Shared layer: `config_loader.py` for S3-based config loading
- External: openFDA API (no auth required)
- AWS: S3 (write), CloudWatch (metrics)

**Configuration Schema:** `config/data_sources/openfda_shortages.json`

```json
{
  "source_name": "openfda_drug_shortages",
  "display_name": "FDA Drug Shortages Database",
  "description": "Current and resolved drug shortage data from FDA",
  "enabled": true,
  "api": {
    "base_url": "https://api.fda.gov/drug/shortages.json",
    "auth_type": "none",
    "rate_limit_per_hour": 240,
    "timeout_seconds": 30,
    "retry_max_attempts": 3,
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

### 2. OpenFDA Response Parser

**Location:** `lambdas/ingestion/openfda_shortage_fetcher/parser.py`

**Responsibility:** Transforms openFDA JSON responses into normalized shortage records.

**Interface:**

```python
from typing import List, Dict, Any

def parse_openfda_response(raw_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts and normalizes shortage records from openFDA API response.

    Input: OpenFDA JSON with structure:
    {
      "results": [
        {
          "product_id": "123",
          "productName": "Amoxicillin Capsules, USP",
          "genericName": "Amoxicillin",
          "currentSupplyStatus": "Available",
          "reason": "Demand increase",
          "estimatedResolutionDate": "2024-03-01"
        }
      ]
    }

    Output: List of normalized records:
    [
      {
        "product_id": "123",
        "product_name": "Amoxicillin Capsules, USP",
        "supply_status": "AVAILABLE",
        "reason_for_shortage": "Demand increase",
        "estimated_resolution_date": "2024-03-01",
        "therapeutic_category": "Antibiotics",
        "week_timestamp": "2024-W03"
      }
    ]

    Field mapping rules:
    - reportsReceived → product_id (primary key)
    - productName → product_name (fallback to genericName if missing)
    - currentSupplyStatus → supply_status (map to AVAILABLE/DISCONTINUED/UNKNOWN)
    - reason → reason_for_shortage
    - estimatedResolutionDate → estimated_resolution_date (nullable)
    - Infer therapeutic_category via pattern matching against config

    Error handling:
    - Missing productName AND genericName: Log warning, skip record
    - Missing product_id: Log error, skip record
    - Unmapped therapeutic category: Assign "Uncategorized", exclude from alerts
    """
    results = raw_response.get("results", [])
    normalized = []

    for record in results:
        if not record.get("product_id"):
            logger.warning(f"Skipping record without product_id: {record}")
            continue

        product_name = record.get("productName") or record.get("genericName")
        if not product_name:
            logger.warning(f"Skipping record without name: {record}")
            continue

        therapeutic_category = infer_therapeutic_category(product_name)

        normalized.append({
            "product_id": record["product_id"],
            "product_name": product_name,
            "supply_status": map_supply_status(record.get("currentSupplyStatus")),
            "reason_for_shortage": record.get("reason", "Unknown"),
            "estimated_resolution_date": record.get("estimatedResolutionDate"),
            "therapeutic_category": therapeutic_category,
            "week_timestamp": get_current_epiweek()
        })

    return normalized
```

### 3. Shortage Change Detector Lambda

**Location:** `lambdas/prediction/shortage_change_detector/`

**Responsibility:** Compares current shortage state against historical DynamoDB records, classifies changes (NEW/WORSENING/RESOLVED/UNCHANGED), and triggers alert generation.

**Interface:**

```python
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Triggered by: S3 PutObject event (via Pipeline Coordinator)

    Input: event = {
        "s3_key": "raw/openfda-shortages/2024/W03/shortages_20240115_060512.json",
        "week_timestamp": "2024-W03"
    }

    Output: {
        "changes_detected": {
            "NEW": 12,
            "WORSENING": 3,
            "RESOLVED": 5,
            "UNCHANGED": 1618
        },
        "alerts_triggered": 15,
        "circuit_breaker_activated": false
    }

    Logic:
    1. Load current shortage data from S3
    2. Query DynamoDB shortage-state table for previous week records
    3. For each product_id in current data:
        a. Compare with previous_week state
        b. Classify: NEW (not in DDB), WORSENING (status degraded),
                     RESOLVED (in DDB but not current), UNCHANGED (same)
    4. Filter by therapeutic categories from config
    5. Check circuit breaker (>20 NEW/WORSENING = require manual review)
    6. Write changes to shortage-alerts table
    7. Invoke Step Functions for NEW/WORSENING shortages
    """
    current_data = load_from_s3(event["s3_key"])
    previous_state = query_dynamodb_state(event["week_timestamp"] - 1)

    changes = classify_changes(current_data, previous_state)
    filtered = filter_by_therapeutic_categories(changes)

    if filtered["NEW"] + filtered["WORSENING"] > 20:
        emit_circuit_breaker_alarm()
        return {"circuit_breaker_activated": true}

    for change in filtered["NEW"] + filtered["WORSENING"]:
        write_alert_record(change)
        trigger_step_functions(change)

    return {"changes_detected": filtered}
```

**Change Classification Logic:**

| Condition                                           | Classification |
| --------------------------------------------------- | -------------- |
| product_id in current BUT NOT in previous DynamoDB  | NEW            |
| supply_status changes from AVAILABLE → DISCONTINUED | WORSENING      |
| reason_for_shortage text changes significantly      | WORSENING      |
| product_id in previous DynamoDB BUT NOT in current  | RESOLVED       |
| All fields match previous week                      | UNCHANGED      |

### 4. Extended Pipeline Coordinator (Orchestration Lambda)

**Location:** `lambdas/orchestration/pipeline_coordinator/` (modified)

**Responsibility:** Routes to disease outbreak OR shortage monitoring paths, enriches disease alerts with shortage context when relevant.

**Interface Extension:**

```python
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    NEW routing logic:

    IF event source is disease surveillance data (Delphi/CDC):
        Execute existing disease outbreak workflow:
            - Leader detection
            - Geographic affinity
            - Timing estimation
            - CHECK: Are relevant medications in shortage? (NEW)
            - IF yes: Enrich alert payload with shortage context
            - Trigger Step Functions with alert_type="combined"
        ELSE:
            - Trigger Step Functions with alert_type="disease_outbreak"

    ELIF event source is openFDA shortage data:
        Execute NEW shortage monitoring workflow:
            - Shortage change detection
            - Filter by therapeutic categories
            - Trigger Step Functions with alert_type="shortage"
    """
    source = identify_data_source(event["s3_key"])

    if source in ["delphi", "cdc_nwss", "cdc_respiratory"]:
        # Existing disease outbreak path
        leader_result = invoke_leader_detection(event)
        if not leader_result["threshold_crossed"]:
            return {"status": "no_alert", "reason": "threshold_not_crossed"}

        affected_counties = invoke_geographic_affinity(leader_result)
        timing_estimates = invoke_timing_estimation(affected_counties)

        # NEW: Check for relevant medication shortages
        disease_key = leader_result["disease_key"]
        relevant_shortages = query_shortage_context(disease_key)

        for county in affected_counties:
            payload = build_disease_alert_payload(county, timing_estimates[county["fips"]])

            if relevant_shortages:
                payload["shortage_context"] = relevant_shortages
                payload["alert_type"] = "combined"
            else:
                payload["alert_type"] = "disease_outbreak"

            start_step_functions(payload)

    elif source == "openfda":
        # NEW shortage monitoring path
        shortage_changes = invoke_shortage_change_detector(event)

        for change in shortage_changes["NEW"] + shortage_changes["WORSENING"]:
            payload = build_shortage_alert_payload(change)
            payload["alert_type"] = "shortage"
            start_step_functions(payload)

    return {"status": "success", "alerts_triggered": count}
```

### 5. Extended Step Functions Workflow

**Location:** `stepfunctions/alert_generation.asl.json` (modified)

**Responsibility:** Conditional branching based on alert_type (disease_outbreak | shortage | combined).

**State Machine Structure:**

```json
{
  "Comment": "Alert Generation with Shortage Intelligence",
  "StartAt": "DetermineAlertType",
  "States": {
    "DetermineAlertType": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.alert_type",
          "StringEquals": "disease_outbreak",
          "Next": "DiseaseOutbreakPath"
        },
        {
          "Variable": "$.alert_type",
          "StringEquals": "shortage",
          "Next": "ShortageAlertPath"
        },
        {
          "Variable": "$.alert_type",
          "StringEquals": "combined",
          "Next": "CombinedSignalPath"
        }
      ],
      "Default": "InvalidAlertType"
    },

    "DiseaseOutbreakPath": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "SituationBrief",
          "States": {
            "SituationBrief": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel"
            },
            "SeverityClassify": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel"
            },
            "ChecklistGenerate": { "Type": "Choice" },
            "CommunicationDrafting": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel"
            }
          }
        }
      ],
      "Next": "DispatchAlert"
    },

    "ShortageAlertPath": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "ShortageSituationBrief",
          "States": {
            "ShortageSituationBrief": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel",
              "Parameters": {
                "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "body": {
                  "anthropic_version": "bedrock-2023-05-31",
                  "system": [
                    {
                      "text": "file://bedrock/prompts/shortage_situation_brief.txt"
                    }
                  ],
                  "messages": [{ "role": "user", "content": "$.shortage_data" }]
                }
              }
            },
            "PharmacistActionGeneration": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel"
            },
            "ShortageCommunicationDrafting": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel"
            }
          }
        }
      ],
      "Next": "DispatchAlert"
    },

    "CombinedSignalPath": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "CombinedSituationBrief",
          "States": {
            "CombinedSituationBrief": {
              "Type": "Task",
              "Resource": "arn:aws:states:::bedrock:invokeModel",
              "Parameters": {
                "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "body": {
                  "system": [
                    {
                      "text": "file://bedrock/prompts/combined_disease_shortage_brief.txt"
                    }
                  ],
                  "messages": [
                    {
                      "role": "user",
                      "content": "$.disease_data + $.shortage_context"
                    }
                  ]
                }
              }
            }
          }
        }
      ],
      "Next": "DispatchAlert"
    },

    "DispatchAlert": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "End": true
    }
  }
}
```

### 6. Extended Subscription API

**Location:** `lambdas/subscription/*` (modified)

**Responsibility:** Extend subscription schema and endpoints to support therapeutic category filtering.

**DynamoDB Schema Extension:**

```python
# EXISTING fields in healthsignals-subscriptions table
{
    "county_fips": "48143",  # Partition key
    "contact_email": "officer@example.com",
    "diseases": ["influenza", "rsv", "covid"],
    "channels": ["email", "sms"],
    "status": "active",
    "verified_at": "2024-01-10T08:30:00Z",

    # NEW field for shortage alerts
    "therapeutic_categories": ["Antivirals", "Antibiotics", "Respiratory"]
}

# NEW GSI: therapeutic-category-lookup
# Partition key: therapeutic_category
# Sort key: county_fips
# Purpose: Efficient querying of subscriptions by therapeutic category for alert routing
```

**API Endpoint Extensions:**

```python
# PUT /preferences — Add therapeutic category subscription
def update_preferences(event, context):
    """
    Input: {
        "county_fips": "48143",
        "therapeutic_categories": ["Antivirals", "Antibiotics"]
    }

    Validation:
    1. Load therapeutic category config from S3
    2. Verify each category_key exists in config
    3. Return HTTP 400 if invalid category provided

    Updates DynamoDB subscriptions table:
    - Appends new categories to therapeutic_categories array
    - Removes duplicates
    """
    body = json.loads(event["body"])
    config = load_therapeutic_category_config()

    for category in body["therapeutic_categories"]:
        if category not in config["categories"]:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Invalid category: {category}"})
            }

    dynamodb.update_item(
        Key={"county_fips": body["county_fips"]},
        UpdateExpression="SET therapeutic_categories = list_append(therapeutic_categories, :new)",
        ExpressionAttributeValues={":new": body["therapeutic_categories"]}
    )

    return {"statusCode": 200, "body": json.dumps({"status": "updated"})}


# GET /status — Include therapeutic category subscriptions
def get_status(event, context):
    """
    Output: {
        "county_fips": "48143",
        "diseases": ["influenza", "rsv"],
        "therapeutic_categories": ["Antivirals", "Antibiotics"],
        "status": "active",
        "last_alert_sent": "2024-01-10T06:25:00Z"
    }
    """
```

### 7. Extended Alert Dispatcher Lambda

**Location:** `lambdas/delivery/alert_dispatcher/` (modified)

**Responsibility:** Filter shortage alerts by therapeutic category subscriptions before delivery.

**Filtering Logic:**

```python
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Extended logic for shortage alerts:

    IF alert_type == "shortage" or alert_type == "combined":
        1. Extract therapeutic_category from alert payload
        2. Query subscriptions table using GSI therapeutic-category-lookup
        3. Filter results: status=active AND verified=true AND not paused
        4. For each matching subscription:
            - Send via SES email (with unsubscribe link + disclaimer)
            - Send via SNS SMS if channel enabled
            - Update last_alert_sent timestamp
    """
    alert_type = event["alert_type"]

    if alert_type in ["shortage", "combined"]:
        therapeutic_category = event.get("therapeutic_category")

        # Query GSI for efficient lookup
        response = dynamodb_client.query(
            TableName="healthsignals-subscriptions",
            IndexName="therapeutic-category-lookup",
            KeyConditionExpression="therapeutic_category = :cat",
            ExpressionAttributeValues={":cat": therapeutic_category},
            FilterExpression="status = :active AND verified = :true AND paused = :false",
            ExpressionAttributeValues={
                ":active": "active",
                ":true": True,
                ":false": False
            }
        )

        subscriptions = response["Items"]

        for sub in subscriptions:
            if "email" in sub["channels"]:
                send_ses_email(
                    to=sub["contact_email"],
                    subject=f"Drug Shortage Alert: {therapeutic_category}",
                    body=render_shortage_email(event),
                    unsubscribe_link=generate_signed_unsubscribe_link(sub)
                )

            if "sms" in sub["channels"] and sub.get("phone_number"):
                send_sns_sms(
                    to=sub["phone_number"],
                    message=render_shortage_sms(event)  # ≤160 chars
                )

            update_last_alert_sent(sub["county_fips"])

    elif alert_type == "disease_outbreak":
        # Existing disease outbreak delivery logic (unchanged)
        pass
```

## Data Models

### DynamoDB Tables

#### 1. healthsignals-drug-shortage-state

**Purpose:** Stores historical shortage state for change detection (NEW/WORSENING/RESOLVED).

**Schema:**

| Attribute                 | Type   | Key       | Description                               |
| ------------------------- | ------ | --------- | ----------------------------------------- |
| product_id                | String | Partition | openFDA product identifier                |
| week_timestamp            | String | Sort      | ISO week (e.g., "2024-W03")               |
| product_name              | String | -         | Drug name from API                        |
| therapeutic_category      | String | GSI PK    | Category (Antivirals, Antibiotics, etc.)  |
| supply_status             | String | -         | AVAILABLE \| DISCONTINUED \| UNKNOWN      |
| reason_for_shortage       | String | -         | FDA reported reason                       |
| estimated_resolution_date | String | -         | Expected resolution (nullable)            |
| shortage_status           | String | -         | NEW \| WORSENING \| RESOLVED \| UNCHANGED |
| previous_supply_status    | String | -         | Status from previous week                 |
| created_at                | String | -         | ISO timestamp                             |
| ttl                       | Number | -         | DynamoDB TTL (52 weeks retention)         |

**Indexes:**

```python
# GSI: therapeutic-category-index
# Partition key: therapeutic_category
# Sort key: week_timestamp
# Purpose: Query all shortages for a category in a specific week
```

**Access Patterns:**

1. Get latest state for product: `Query(PK=product_id, SK begins_with current_week)`
2. Get all shortages for category: `Query(GSI, PK=therapeutic_category, SK=week)`
3. Historical lookup: `Query(PK=product_id, SK between week_start and week_end)`

**Retention:** 52 weeks via DynamoDB TTL (set `ttl = current_timestamp + 52 weeks`)

#### 2. healthsignals-shortage-alerts

**Purpose:** Tracks alert generation for idempotency (prevents duplicate alerts for same shortage+week).

**Schema:**

| Attribute                   | Type   | Key       | Description                     |
| --------------------------- | ------ | --------- | ------------------------------- |
| product_id                  | String | Partition | openFDA product identifier      |
| week_timestamp              | String | Sort      | ISO week (e.g., "2024-W03")     |
| therapeutic_category        | String | -         | Category for filtering          |
| shortage_status             | String | -         | NEW \| WORSENING \| RESOLVED    |
| detection_timestamp         | String | -         | When change was detected        |
| alert_generated             | String | -         | PENDING \| SENT \| FAILED       |
| step_function_execution_arn | String | -         | SFN execution ID for tracing    |
| delivery_timestamp          | String | -         | When alert was delivered        |
| recipients_count            | Number | -         | Number of subscriptions matched |
| error_message               | String | -         | If alert_generated=FAILED       |
| retry_count                 | Number | -         | Number of retry attempts        |
| created_at                  | String | -         | ISO timestamp                   |

**Access Patterns:**

1. Check if alert already sent: `GetItem(PK=product_id, SK=week_timestamp)`
2. Find failed alerts for retry: `Scan(FilterExpression="alert_generated = FAILED AND retry_count < 3")`
3. Manual regeneration: Delete record, re-run change detector

**Idempotency Logic:**

```python
def should_generate_alert(product_id: str, week: str) -> bool:
    """
    Returns True only if:
    1. No record exists for (product_id, week), OR
    2. Record exists with alert_generated=FAILED and retry_count < 3
    """
    response = dynamodb.get_item(
        Key={"product_id": product_id, "week_timestamp": week}
    )

    if "Item" not in response:
        return True  # No record, first attempt

    item = response["Item"]
    if item["alert_generated"] == "SENT":
        return False  # Already sent, skip

    if item["alert_generated"] == "FAILED" and item["retry_count"] < 3:
        return True  # Failed, retry allowed

    return False  # Failed 3+ times, stop retrying
```

#### 3. healthsignals-subscriptions (Extended)

**Purpose:** Existing table extended with therapeutic_categories field and new GSI.

**Schema Changes:**

```python
# EXISTING fields (unchanged):
{
    "county_fips": "48143",  # Partition key
    "contact_email": "officer@example.com",
    "contact_name": "Jane Smith",
    "phone_number": "+15125551234",
    "diseases": ["influenza", "rsv", "covid"],
    "channels": ["email", "sms"],
    "status": "active",
    "verified": true,
    "verified_at": "2024-01-10T08:30:00Z",
    "paused": false,
    "last_alert_sent": "2024-01-15T06:25:00Z",
    "created_at": "2024-01-05T12:00:00Z"
}

# NEW field:
{
    "therapeutic_categories": ["Antivirals", "Antibiotics", "Respiratory"]
    # Default: [] (empty array if not specified during subscription creation)
}
```

**New Index:**

```python
# GSI: therapeutic-category-lookup
# Partition key: therapeutic_category (String)
# Sort key: county_fips (String)
# Purpose: Efficiently route shortage alerts to matching subscriptions
# Projection: ALL (need full subscription record for delivery)
```

**Access Patterns:**

1. Find subscriptions for category: `Query(GSI, PK=therapeutic_category, FilterExpression="status=active AND verified=true")`
2. Update preferences: `UpdateItem(PK=county_fips, SET therapeutic_categories)`
3. Get subscription status: `GetItem(PK=county_fips)`

**Migration Strategy:**

```python
# For existing subscriptions, set therapeutic_categories=[] (empty array)
# No alert delivery until user explicitly subscribes to categories via PUT /preferences
```

### Configuration Schemas

#### Therapeutic Categories Configuration

**Location:** `config/shortage_monitoring/therapeutic_categories.json`

**Purpose:** Defines monitored drug categories, FDA classification mappings, and relationships to diseases.

**Schema:**

```json
{
  "version": "1.0",
  "last_updated": "2024-01-15",
  "categories": [
    {
      "category_key": "Antivirals",
      "display_name": "Antiviral Medications",
      "description": "Medications used to treat viral infections",
      "priority_level": "HIGH",
      "relevant_diseases": ["influenza", "covid"],
      "fda_classification_mapping": [
        "Antiviral*",
        "*oseltamivir*",
        "*Tamiflu*",
        "*baloxavir*",
        "*nirmatrelvir*",
        "*Paxlovid*"
      ]
    },
    {
      "category_key": "Antibiotics",
      "display_name": "Antibiotic Medications",
      "description": "Medications used to treat bacterial infections",
      "priority_level": "HIGH",
      "relevant_diseases": [],
      "fda_classification_mapping": [
        "Antibiotic*",
        "*Amoxicillin*",
        "*Azithromycin*",
        "*Penicillin*",
        "*Cephalosporin*"
      ]
    },
    {
      "category_key": "Respiratory",
      "display_name": "Respiratory Medications",
      "description": "Inhalers, bronchodilators, and respiratory treatments",
      "priority_level": "MEDIUM",
      "relevant_diseases": ["rsv", "influenza"],
      "fda_classification_mapping": [
        "*Albuterol*",
        "*Inhaler*",
        "*Nebulizer*",
        "*Bronchodilator*"
      ]
    }
  ],
  "priority_levels": {
    "HIGH": "Alert generated immediately, routed to all matching subscriptions",
    "MEDIUM": "Alert generated, standard routing",
    "LOW": "Alert generated only if multiple shortages in category"
  }
}
```

**Field Definitions:**

- `category_key`: Unique identifier used in DynamoDB and API requests
- `display_name`: Human-readable name shown in alerts
- `priority_level`: HIGH | MEDIUM | LOW (affects alert generation logic)
- `relevant_diseases`: Array of disease_key values from `config/diseases/*.json`
- `fda_classification_mapping`: Regex patterns for matching openFDA product names

## API Integration: OpenFDA Drug Shortages

### Endpoint Details

**Base URL:** `https://api.fda.gov/drug/shortages.json`

**Authentication:** None required (public API)

**Rate Limits:**

- 240 requests per hour per IP address
- 1,000 requests per day per IP address (burst tolerance)
- No API key required for this limit

**Pagination:**

```bash
# Example requests
GET https://api.fda.gov/drug/shortages.json?limit=1000&skip=0
GET https://api.fda.gov/drug/shortages.json?limit=1000&skip=1000
```

**Response Structure:**

```json
{
  "meta": {
    "disclaimer": "Do not rely on openFDA to make decisions regarding medical care...",
    "terms": "https://open.fda.gov/terms/",
    "license": "https://open.fda.gov/license/",
    "last_updated": "2024-01-15",
    "results": {
      "skip": 0,
      "limit": 1000,
      "total": 1638
    }
  },
  "results": [
    {
      "product_id": "1234",
      "productName": "Amoxicillin Capsules, USP",
      "genericName": "Amoxicillin",
      "currentSupplyStatus": "Available",
      "reason": "Demand increase for this drug",
      "estimatedResolutionDate": "2024-03-01",
      "availableSupply": "Limited supply available",
      "manufacturer": "Teva Pharmaceuticals",
      "revisionDate": "2024-01-10"
    }
  ]
}
```

### Error Handling Strategy

| Error Code                    | Condition           | Action                                            |
| ----------------------------- | ------------------- | ------------------------------------------------- |
| **429 Too Many Requests**     | Rate limit exceeded | Exponential backoff: 5s, 10s, 20s, then SQS retry |
| **500 Internal Server Error** | FDA API issues      | Retry with exponential backoff (max 3 attempts)   |
| **503 Service Unavailable**   | Temporary outage    | Retry with backoff, fail to DLQ after 3 attempts  |
| **404 Not Found**             | Endpoint changed    | Log error, skip retry, alert operations team      |
| **Timeout (>30s)**            | Network issues      | Retry via SQS visibility timeout                  |

### Retry Logic Implementation

```python
import time
import requests
from typing import Dict, Any

def fetch_with_retry(
    url: str,
    max_retries: int = 3,
    timeout: int = 30
) -> Dict[str, Any]:
    """
    Fetches openFDA data with exponential backoff retry logic.

    Backoff schedule:
    - Attempt 1: No delay
    - Attempt 2: 5 seconds
    - Attempt 3: 10 seconds
    - Attempt 4: 20 seconds
    """
    backoff_delays = [0, 5, 10, 20]

    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=timeout)

            if response.status_code == 200:
                return response.json()

            elif response.status_code == 429:
                logger.warning(f"Rate limit hit (429), attempt {attempt+1}/{max_retries}")
                emit_metric("openfda_rate_limit_errors", 1)
                time.sleep(backoff_delays[attempt])
                continue

            elif response.status_code in [500, 503]:
                logger.warning(f"Server error {response.status_code}, attempt {attempt+1}/{max_retries}")
                time.sleep(backoff_delays[attempt])
                continue

            elif response.status_code == 404:
                logger.error("openFDA endpoint not found (404) - may have changed")
                raise EndpointNotFoundError("FDA API endpoint returned 404")

            else:
                logger.error(f"Unexpected status code: {response.status_code}")
                raise UnexpectedStatusCodeError(response.status_code)

        except requests.Timeout:
            logger.warning(f"Request timeout, attempt {attempt+1}/{max_retries}")
            time.sleep(backoff_delays[attempt])
            continue

        except requests.RequestException as e:
            logger.error(f"Request failed: {str(e)}")
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff_delays[attempt])

    raise MaxRetriesExceededError(f"Failed after {max_retries} attempts")
```

### Circuit Breaker Pattern

```python
def check_circuit_breaker(new_count: int, worsening_count: int) -> bool:
    """
    Circuit breaker prevents mass alerting from data quality issues.

    Threshold: >20 NEW or WORSENING shortages in a single week

    Action if triggered:
    1. Emit CloudWatch alarm (SNS notification to operations team)
    2. Log detailed change summary for manual review
    3. Skip automatic alert generation
    4. Require manual approval to proceed
    """
    threshold = 20
    total_changes = new_count + worsening_count

    if total_changes > threshold:
        logger.error(
            f"Circuit breaker ACTIVATED: {total_changes} changes detected "
            f"(NEW={new_count}, WORSENING={worsening_count})"
        )

        emit_cloudwatch_alarm(
            alarm_name="DrugShortageCircuitBreaker",
            message=f"Manual review required: {total_changes} shortage changes detected"
        )

        # Write to DynamoDB for manual review
        dynamodb.put_item(
            TableName="healthsignals-circuit-breaker-events",
            Item={
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.utcnow().isoformat(),
                "change_count": total_changes,
                "new_count": new_count,
                "worsening_count": worsening_count,
                "status": "PENDING_REVIEW",
                "approved_by": None,
                "approved_at": None
            }
        )

        return True  # Circuit breaker activated

    return False  # Proceed with alert generation
```

## Alert Generation Flow

### Bedrock Prompt Engineering

#### 1. Shortage Situation Brief Prompt

**Location:** `bedrock/prompts/shortage_situation_brief.txt`

**Purpose:** Generate pharmacist-focused shortage summaries following CDC CERC principles.

**Content:**

```
You are a pharmaceutical supply chain analyst generating shortage alerts for rural health facility pharmacists.

Your role:
- Summarize drug shortage situations clearly and accurately
- Highlight affected medications, supply status, and resolution timelines
- Provide context without clinical recommendations
- Use non-alarmist, factual language consistent with CDC CERC principles

Structure your brief:
1. Executive Summary (2-3 sentences)
2. Affected Medications (list with supply status)
3. Supply Status Details (current availability, manufacturer info)
4. Resolution Timeline (estimated date if available, or "Unknown")
5. Pharmacist Actions (inventory review, communication with prescribers)

Critical requirements:
- DO NOT provide specific drug substitution recommendations
- DO NOT offer clinical guidance
- ALWAYS include disclaimer: "FOR PHARMACIST REVIEW ONLY"
- Use clear, direct language
- Avoid technical jargon where possible
- If resolution timeline unknown, state this clearly

Example structure:
---
FOR PHARMACIST REVIEW ONLY

EXECUTIVE SUMMARY
[2-3 sentence overview of shortage situation]

AFFECTED MEDICATIONS
• [Drug name] - [Supply status]
• [Drug name] - [Supply status]

SUPPLY STATUS
[Detailed status with manufacturer info]

RESOLUTION TIMELINE
[Estimated resolution or "Timeline unknown - monitor FDA updates"]

PHARMACIST ACTIONS
• Review current inventory levels
• Communicate with prescribers about availability
• Consider conservation strategies
• Monitor FDA shortage database for updates
---

Input data will be provided as JSON with structure:
{
  "therapeutic_category": "Antivirals",
  "affected_products": [...],
  "week_timestamp": "2024-W03"
}
```

#### 2. Combined Disease + Shortage Alert Prompt

**Location:** `bedrock/prompts/combined_disease_shortage_brief.txt`

**Purpose:** Enrich disease outbreak alerts with drug availability context.

**Content:**

```
You are generating a combined disease outbreak and drug shortage alert for rural health officers.

Your role:
- Integrate disease outbreak information with medication availability context
- Prioritize disease outbreak details while providing critical shortage context
- Help health officers understand supply chain constraints affecting outbreak response

Structure your brief:
1. Disease Outbreak Summary (primary focus)
   - Disease, affected metro, expected timeline
   - Projected severity for recipient county
2. Medication Availability Alert (supplemental section)
   - Affected medications relevant to this disease
   - Supply status
   - Implications for patient care capacity
3. Combined Preparedness Actions
   - Standard outbreak preparations
   - Modified approaches due to medication constraints

Critical requirements:
- Disease outbreak information is PRIMARY content
- Medication availability is SUPPLEMENTAL context
- DO NOT provide specific drug substitution recommendations
- Include disclaimer: "FOR PHARMACIST REVIEW ONLY - Medication availability information"
- Maintain CDC CERC communication principles

Example structure:
---
DISEASE OUTBREAK ALERT

[Standard disease outbreak brief content]

MEDICATION AVAILABILITY ALERT
FOR PHARMACIST REVIEW ONLY

The following medications relevant to [disease] management are currently experiencing supply shortages:

• [Drug name] - [Status]
• [Drug name] - [Status]

IMPLICATIONS FOR OUTBREAK PREPAREDNESS
• [Impact on patient care capacity]
• [Recommended inventory actions]
• [Communication with clinical staff]

COMBINED PREPAREDNESS ACTIONS
1. [Standard outbreak preparation]
2. [Modified approach due to shortages]
3. [Inventory and supply chain coordination]
---

Input data structure:
{
  "disease_data": {
    "disease_name": "Influenza",
    "metro_leader": "Houston",
    "county_fips": "48143",
    "severity": "MODERATE",
    ...
  },
  "shortage_context": {
    "therapeutic_category": "Antivirals",
    "affected_products": [...],
    "supply_status": "LIMITED"
  }
}
```

### Knowledge Base Content

#### Shortage Guidance Documents

**Location:** `bedrock/knowledge_bases/shortage_guidance/`

**Purpose:** Provide Bedrock with authoritative guidance for shortage alert generation.

**Documents:**

1. **fda_shortage_protocols.md** (~8 KB)
   - FDA guidance on managing drug shortages
   - Conservation strategies (dose optimization, therapeutic interchange frameworks)
   - Communication protocols with prescribers
   - Patient safety considerations during shortages
   - Compounding alternatives (general frameworks, not specific recipes)

2. **therapeutic_substitution_framework.md** (~6 KB)
   - General principles for therapeutic substitution
   - When to involve clinical pharmacists
   - Contraindications and special populations
   - Documentation requirements
   - **Important:** Does NOT provide specific drug-to-drug substitutions

3. **inventory_management_strategies.md** (~5 KB)
   - Proactive shortage monitoring best practices
   - Just-in-time inventory adjustments
   - Communication with group purchasing organizations (GPOs)
   - Multi-source product identification
   - Emergency supply coordination

**Knowledge Base Configuration:**

```json
{
  "knowledge_base_id": "shortage-guidance-kb",
  "retrieval_strategy": "precision",
  "retrieval_parameters": {
    "top_k": 3,
    "relevance_threshold": 0.7,
    "max_tokens_per_chunk": 500
  },
  "chunking_strategy": {
    "type": "fixed_size",
    "chunk_size": 500,
    "overlap": 50
  }
}
```

**Total Size:** ~19 KB (well under 30 KB latency threshold)

### Step Functions Workflow Execution

**Shortage Alert Path:**

```
┌─────────────────────────────────────────────────────────────────┐
│  Input: {                                                        │
│    "alert_type": "shortage",                                    │
│    "therapeutic_category": "Antivirals",                        │
│    "affected_products": [                                       │
│      {                                                           │
│        "product_id": "1234",                                    │
│        "product_name": "Oseltamivir Capsules",                 │
│        "supply_status": "DISCONTINUED",                        │
│        "reason": "Manufacturing delay"                         │
│      }                                                           │
│    ],                                                           │
│    "shortage_status": "WORSENING",                             │
│    "week_timestamp": "2024-W03"                                │
│  }                                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 1: Shortage Situation Brief                               │
│  Model: Claude Sonnet 4.5                                       │
│  KB: shortage-guidance-kb (precision retrieval, top-3)          │
│                                                                  │
│  Output: {                                                      │
│    "brief": "FOR PHARMACIST REVIEW ONLY\n\n..."                │
│  }                                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 2: Pharmacist Action Generation                           │
│  Model: Claude Sonnet 4.5                                       │
│                                                                  │
│  Output: {                                                      │
│    "actions": [                                                 │
│      "Review current inventory of oseltamivir",                │
│      "Communicate availability to prescribers",                │
│      "Monitor FDA database for resolution updates"             │
│    ]                                                            │
│  }                                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 3: Communication Drafting                                 │
│  Model: Claude Sonnet 4.5                                       │
│  KB: communication-templates-kb (variety retrieval, top-8)      │
│                                                                  │
│  Output: {                                                      │
│    "email_subject": "Drug Shortage Alert: Antivirals",         │
│    "email_body": "...",                                        │
│    "sms_summary": "Antiviral shortage: Oseltamivir..."         │
│  }                                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Dispatch Alert Lambda                                           │
│  • Query subscriptions by therapeutic_category GSI              │
│  • Filter: active + verified + not paused                       │
│  • Send via SES email + SNS SMS                                 │
│  • Update shortage-alerts table: alert_generated=SENT           │
└─────────────────────────────────────────────────────────────────┘
```

**Token Economics (Shortage Alert):**

| Step                | Input Tokens | Output Tokens | Total      |
| ------------------- | ------------ | ------------- | ---------- |
| 1. Situation Brief  | ~1,200       | ~600          | 1,800      |
| 2. Actions          | ~900         | ~400          | 1,300      |
| 3. Communication    | ~1,500       | ~800          | 2,300      |
| **Total per alert** | **~3,600**   | **~1,800**    | **~5,400** |

**Cost per shortage alert:** ~$0.038 (3,600 × $3/MTok + 1,800 × $15/MTok)

## Integration Points

### Extending Pipeline Coordinator

**File:** `lambdas/orchestration/pipeline_coordinator/handler.py`

**Changes Required:**

```python
# EXISTING disease outbreak logic (unchanged):
def handle_disease_surveillance_data(s3_key: str) -> Dict[str, Any]:
    """Existing function for Delphi/CDC disease data"""
    leader_result = invoke_leader_detection(s3_key)
    if not leader_result["threshold_crossed"]:
        return {"status": "no_alert"}

    affected_counties = invoke_geographic_affinity(leader_result)
    timing_estimates = invoke_timing_estimation(affected_counties)

    # NEW: Check for shortage context
    disease_key = leader_result["disease_key"]
    shortage_context = query_shortage_context(disease_key)

    for county in affected_counties:
        payload = build_alert_payload(county, timing_estimates)

        if shortage_context:
            payload["shortage_context"] = shortage_context
            payload["alert_type"] = "combined"
        else:
            payload["alert_type"] = "disease_outbreak"

        start_step_functions(payload)

    return {"status": "success", "alerts_triggered": len(affected_counties)}


# NEW: Shortage monitoring logic
def handle_shortage_data(s3_key: str) -> Dict[str, Any]:
    """New function for openFDA shortage data"""
    # Invoke shortage change detector Lambda
    detector_response = lambda_client.invoke(
        FunctionName="healthsignals-shortage-change-detector",
        InvocationType="RequestResponse",
        Payload=json.dumps({"s3_key": s3_key})
    )

    result = json.loads(detector_response["Payload"].read())

    # Changes are already filtered and written to DDB by detector
    # Detector also invokes Step Functions for each NEW/WORSENING shortage

    return {
        "status": "success",
        "changes_detected": result["changes_detected"],
        "alerts_triggered": result["alerts_triggered"]
    }


# UPDATED: Main handler routing
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Route to appropriate handler based on data source"""
    s3_key = event["Records"][0]["s3"]["object"]["key"]

    if "openfda-shortages" in s3_key:
        return handle_shortage_data(s3_key)
    elif any(src in s3_key for src in ["delphi", "cdc-nwss", "cdc-respiratory"]):
        return handle_disease_surveillance_data(s3_key)
    else:
        logger.warning(f"Unknown data source in S3 key: {s3_key}")
        return {"status": "error", "reason": "unknown_source"}


# NEW: Query shortage context for disease enrichment
def query_shortage_context(disease_key: str) -> Optional[Dict[str, Any]]:
    """
    Check if any relevant medications for this disease are in shortage.

    Logic:
    1. Load therapeutic category config
    2. Find categories with disease_key in relevant_diseases array
    3. Query shortage-state table for current week NEW/WORSENING
    4. Return shortage summary if found
    """
    config = load_therapeutic_category_config()
    relevant_categories = [
        cat for cat in config["categories"]
        if disease_key in cat["relevant_diseases"]
    ]

    if not relevant_categories:
        return None

    current_week = get_current_epiweek()
    shortages = []

    for category in relevant_categories:
        response = dynamodb.query(
            TableName="healthsignals-drug-shortage-state",
            IndexName="therapeutic-category-index",
            KeyConditionExpression="therapeutic_category = :cat AND week_timestamp = :week",
            FilterExpression="shortage_status IN (:new, :worsening)",
            ExpressionAttributeValues={
                ":cat": category["category_key"],
                ":week": current_week,
                ":new": "NEW",
                ":worsening": "WORSENING"
            }
        )
        shortages.extend(response["Items"])

    if not shortages:
        return None

    return {
        "therapeutic_categories": [cat["category_key"] for cat in relevant_categories],
        "affected_products": shortages,
        "shortage_count": len(shortages)
    }
```

### Combined Signal Logic

**Decision Flow:**

```
┌─────────────────────────────────────────────────────────────┐
│  Disease Outbreak Detected (Leader crosses threshold)       │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
                    ┌─────────┐
                    │ Disease │
                    │   Key   │ (e.g., "influenza")
                    └────┬────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Query: therapeutic_categories config                        │
│  Find categories where relevant_diseases contains disease_key│
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Relevant Categories? │
              └──────────┬───────────┘
                         │
            ┌────────────┴────────────┐
            │                         │
          YES                        NO
            │                         │
            ▼                         ▼
┌──────────────────────┐    ┌──────────────────────┐
│ Query shortage-state │    │ Standard Disease     │
│ table for current    │    │ Outbreak Alert       │
│ week NEW/WORSENING   │    │ (no enrichment)      │
└──────────┬───────────┘    └──────────────────────┘
           │
           ▼
    ┌──────────────┐
    │ Shortages    │
    │ Found?       │
    └──────┬───────┘
           │
      ┌────┴────┐
      │         │
     YES       NO
      │         │
      ▼         ▼
┌──────────┐  ┌─────────────┐
│ Combined │  │ Standard    │
│ Signal   │  │ Disease     │
│ Alert    │  │ Alert       │
└──────────┘  └─────────────┘
```

**Example Combined Signal:**

```json
{
  "alert_type": "combined",
  "disease_data": {
    "disease_key": "influenza",
    "disease_name": "Influenza",
    "metro_leader": "Houston",
    "county_fips": "48143",
    "county_name": "Erath County",
    "severity": "MODERATE",
    "estimated_arrival_week": "2024-W06",
    "peak_projection": 2.3
  },
  "shortage_context": {
    "therapeutic_categories": ["Antivirals"],
    "affected_products": [
      {
        "product_name": "Oseltamivir Capsules 75mg",
        "supply_status": "DISCONTINUED",
        "reason": "Manufacturing delay",
        "therapeutic_category": "Antivirals"
      },
      {
        "product_name": "Baloxavir Marboxil Tablets",
        "supply_status": "AVAILABLE",
        "reason": "Demand increase (limited supply)",
        "therapeutic_category": "Antivirals"
      }
    ],
    "shortage_count": 2,
    "supply_impact": "MODERATE"
  }
}
```

## Error Handling Strategy

### Lambda Error Handling

| Component                    | Error Type                   | Recovery Strategy                               |
| ---------------------------- | ---------------------------- | ----------------------------------------------- |
| **OpenFDA Fetcher**          | HTTP 429 rate limit          | Exponential backoff: 5s, 10s, 20s               |
|                              | HTTP 500/503 server error    | Retry with backoff (max 3 attempts)             |
|                              | HTTP 404 not found           | Log error, skip retry, alert ops team           |
|                              | Network timeout (>30s)       | SQS visibility timeout retry                    |
|                              | Parse error (malformed JSON) | Log error, send to DLQ, continue                |
| **Shortage Change Detector** | DynamoDB throttling          | Exponential backoff with jitter (max 5 retries) |
|                              | Missing previous week data   | Treat all as NEW (first-time monitoring)        |
|                              | Circuit breaker activated    | Skip alert generation, emit alarm               |
| **Pipeline Coordinator**     | Lambda invocation failure    | Retry via SQS (up to 3 attempts)                |
|                              | Step Functions throttling    | Queue for delayed retry (60s)                   |
| **Step Functions**           | Bedrock throttling           | Exponential backoff (5s, 10s, 20s, 40s)         |
|                              | Bedrock model error          | Retry once, then fail to DLQ                    |
|                              | KB retrieval failure         | Proceed without KB context, log warning         |
| **Alert Dispatcher**         | SES sending failure          | Retry with exponential backoff                  |
|                              | SNS SMS failure              | Log error, proceed (email still sent)           |
|                              | No subscriptions found       | Log event, skip delivery                        |

### Graceful Degradation

```python
def handle_partial_failure(component: str, error: Exception) -> None:
    """
    Graceful degradation strategy:
    1. Log error with full context
    2. Emit CloudWatch metric
    3. Store raw data for manual recovery
    4. Continue processing remaining items
    """
    logger.error(f"{component} partial failure: {str(error)}", exc_info=True)

    emit_metric(f"{component}_partial_failure", 1)

    # Store failure context to S3 for manual review
    s3_client.put_object(
        Bucket=config["data_bucket"],
        Key=f"errors/{component}/{datetime.utcnow().isoformat()}.json",
        Body=json.dumps({
            "component": component,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "timestamp": datetime.utcnow().isoformat()
        })
    )

    # If shortage detection fails, still store raw openFDA data to S3
    # Manual recovery possible by reprocessing S3 data
```

### Dead Letter Queue (DLQ) Processing

**DLQ Configuration:**

- Retention: 14 days
- Alarm: CloudWatch alarm fires if message count > 0
- Manual review: Operations team investigates within 24 hours

**DLQ Message Structure:**

```json
{
  "original_event": {
    "source": "scheduled",
    "trigger": "weekly_monday"
  },
  "error_details": {
    "error_type": "MaxRetriesExceededError",
    "error_message": "Failed after 3 attempts",
    "timestamp": "2024-01-15T06:10:00Z",
    "lambda_request_id": "abc-123-def-456"
  },
  "retry_attempts": 3,
  "final_status_code": 503
}
```

## Security Design

### IAM Roles and Permissions

#### OpenFDA Fetcher Lambda Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3DataLakeWrite",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:PutObjectAcl"],
      "Resource": "arn:aws:s3:::healthsignals-data-*/raw/openfda-shortages/*"
    },
    {
      "Sid": "S3ConfigRead",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::healthsignals-data-*/config/*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/healthsignals-openfda-*"
    },
    {
      "Sid": "CloudWatchMetrics",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "HealthSignals/DrugShortages"
        }
      }
    },
    {
      "Sid": "SQSReceive",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:*:*:healthsignals-openfda-shortages-queue"
    }
  ]
}
```

#### Shortage Change Detector Lambda Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3DataRead",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::healthsignals-data-*/raw/openfda-shortages/*"
    },
    {
      "Sid": "S3ConfigRead",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::healthsignals-data-*/config/*"
    },
    {
      "Sid": "DynamoDBShortageState",
      "Effect": "Allow",
      "Action": [
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:*:table/healthsignals-drug-shortage-state",
        "arn:aws:dynamodb:*:*:table/healthsignals-drug-shortage-state/index/*"
      ]
    },
    {
      "Sid": "DynamoDBShortageAlerts",
      "Effect": "Allow",
      "Action": [
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/healthsignals-shortage-alerts"
    },
    {
      "Sid": "StepFunctionsInvoke",
      "Effect": "Allow",
      "Action": ["states:StartExecution"],
      "Resource": "arn:aws:states:*:*:stateMachine:healthsignals-alert-generation"
    }
  ]
}
```

### Encryption

| Component                      | Encryption Method      | Key Management                 |
| ------------------------------ | ---------------------- | ------------------------------ |
| **S3 (raw shortage data)**     | SSE-S3 (AES-256)       | AWS-managed keys               |
| **DynamoDB (shortage-state)**  | Encryption at rest     | AWS-managed keys (default)     |
| **DynamoDB (shortage-alerts)** | Encryption at rest     | AWS-managed keys (default)     |
| **SQS messages**               | Server-side encryption | AWS-managed keys               |
| **CloudWatch Logs**            | Encryption at rest     | AWS-managed keys               |
| **Secrets Manager**            | Encryption at rest     | AWS KMS (customer-managed key) |
| **Data in transit**            | TLS 1.2+               | AWS Certificate Manager        |

**Rationale for AWS-managed keys:**

- No PHI/PII stored (aggregate drug shortage data only)
- Cost optimization (no KMS charges for AWS-managed keys)
- Simplified key rotation (automatic)
- Compliance: Sufficient for non-HIPAA, non-PHI workloads

### Input Validation

```python
def validate_therapeutic_category(category: str) -> bool:
    """
    Validates therapeutic category against loaded config.
    Prevents injection attacks via category filtering.
    """
    config = load_therapeutic_category_config()
    valid_categories = [cat["category_key"] for cat in config["categories"]]

    if category not in valid_categories:
        logger.warning(f"Invalid therapeutic category: {category}")
        return False

    return True


def sanitize_product_name(product_name: str) -> str:
    """
    Sanitizes product names from openFDA API before storage.
    Removes potentially dangerous characters.
    """
    # Remove HTML/JavaScript injection patterns
    sanitized = re.sub(r'[<>\'\"\\]', '', product_name)

    # Limit length to prevent DynamoDB attribute size issues
    return sanitized[:500]


def validate_week_timestamp(week: str) -> bool:
    """
    Validates ISO week format (YYYY-Www).
    Prevents path traversal via week-based S3 keys.
    """
    pattern = r'^\d{4}-W\d{2}$'
    if not re.match(pattern, week):
        logger.warning(f"Invalid week format: {week}")
        return False

    # Validate week number range (1-53)
    week_num = int(week.split('-W')[1])
    if not (1 <= week_num <= 53):
        return False

    return True
```

### Bedrock Guardrails Extension

**Existing Guardrails:** `bedrock/guardrails/healthsignals_guardrail.json`

**Additional Denied Topics for Shortage Alerts:**

```json
{
  "guardrailId": "healthsignals-guardrail",
  "deniedTopics": [
    {
      "name": "DrugSubstitutionRecommendations",
      "definition": "Specific drug-to-drug substitution recommendations or clinical guidance on medication alternatives",
      "examples": [
        "Use amoxicillin instead of penicillin",
        "Switch patients from oseltamivir to baloxavir",
        "Substitute drug X with drug Y for this condition"
      ],
      "type": "DENY"
    },
    {
      "name": "CompoundingInstructions",
      "definition": "Specific instructions for compounding medications or preparing alternative formulations",
      "examples": [
        "Compound tablets from powder using this ratio",
        "Prepare IV solution by mixing these ingredients",
        "Create suspension using crushed tablets"
      ],
      "type": "DENY"
    },
    {
      "name": "DosageAdjustmentAdvice",
      "definition": "Specific dosage adjustment recommendations during shortages",
      "examples": [
        "Reduce dosage to 50mg to conserve supply",
        "Extend dosing interval from q6h to q8h",
        "Split tablets to make supply last longer"
      ],
      "type": "DENY"
    }
  ],
  "wordFilters": [
    {
      "text": "substitute",
      "action": "BLOCK"
    },
    {
      "text": "switch to",
      "action": "BLOCK"
    },
    {
      "text": "use instead",
      "action": "BLOCK"
    },
    {
      "text": "compound using",
      "action": "BLOCK"
    }
  ]
}
```

**Important:** Guardrails are configured but not activated by default. Requires manual activation via AWS Console (see DEPLOYMENT.md Step 7).

### Audit and Compliance

**CloudTrail Logging:**

- All DynamoDB operations (PutItem, UpdateItem, Query)
- All S3 operations (PutObject, GetObject)
- All Lambda invocations
- All Step Functions executions
- All Bedrock InvokeModel calls

**Retention:**

- CloudTrail logs: 90 days in CloudWatch Logs, archived to S3 indefinitely
- Lambda logs: 30 days in CloudWatch Logs
- X-Ray traces: 30 days

**No PHI/PII Considerations:**

- OpenFDA data contains NO patient information
- Shortages are aggregate, product-level data only
- Contact information in subscriptions table is NOT health data
- System does NOT fall under HIPAA scope

**Data Sovereignty:**

- All data stored in single AWS region (us-east-1 by default)
- No cross-region replication
- No international data transfer

## Testing Strategy

### Unit Tests

**Coverage Target:** 80% code coverage for Lambda business logic

**Test Files:**

1. `tests/unit/test_openfda_parser.py`
   - Test normalization of valid openFDA responses
   - Test handling of missing productName (fallback to genericName)
   - Test handling of missing product_id (skip record)
   - Test therapeutic category inference from product names
   - Test supply status mapping (Available → AVAILABLE, etc.)

2. `tests/unit/test_shortage_change_detector.py`
   - Test classification: NEW (not in DDB)
   - Test classification: WORSENING (status change AVAILABLE → DISCONTINUED)
   - Test classification: RESOLVED (in DDB but not current)
   - Test classification: UNCHANGED (all fields match)
   - Test therapeutic category filtering
   - Test circuit breaker activation (>20 changes)

3. `tests/unit/test_pipeline_coordinator_extensions.py`
   - Test routing to shortage handler for openFDA S3 keys
   - Test shortage context query for disease enrichment
   - Test combined signal payload building
   - Test standard disease alert when no shortages exist

4. `tests/unit/test_subscription_api_extensions.py`
   - Test therapeutic_categories field validation
   - Test invalid category rejection (HTTP 400)
   - Test preference update with category additions
   - Test status endpoint includes therapeutic_categories

### Integration Tests

**Test Fixtures:** `tests/data/openfda_mock_responses.json`

```json
{
  "scenario_1_new_shortages": {
    "meta": {...},
    "results": [
      {
        "product_id": "TEST-001",
        "productName": "Amoxicillin Capsules 500mg",
        "currentSupplyStatus": "Discontinued",
        "reason": "Manufacturing delay"
      }
    ]
  },
  "scenario_2_resolved_shortages": {
    "meta": {...},
    "results": []
  },
  "scenario_3_malformed_data": {
    "results": [
      {
        "product_id": null,
        "productName": null
      }
    ]
  }
}
```

**Integration Test Cases:**

1. `tests/integration/test_shortage_ingestion_flow.py`
   - Mock openFDA API with test fixtures
   - Trigger Lambda via test event
   - Verify S3 object written to correct prefix
   - Verify CloudWatch metrics emitted
   - Test retry logic with 503 server error
   - Test DLQ message for failed API calls

2. `tests/integration/test_shortage_change_detection_flow.py`
   - Seed DynamoDB with previous week state
   - Upload test shortage data to S3
   - Trigger change detector Lambda
   - Verify DynamoDB records written to shortage-state table
   - Verify shortage-alerts records created for NEW/WORSENING
   - Verify Step Functions executions started

3. `tests/integration/test_combined_signal_generation.py`
   - Trigger disease outbreak detection
   - Seed shortage-state table with relevant medication shortages
   - Verify Pipeline Coordinator queries shortage context
   - Verify Step Functions receives combined payload
   - Verify Bedrock receives both disease and shortage data

4. `tests/integration/test_subscription_filtering.py`
   - Create test subscriptions with therapeutic_categories
   - Trigger shortage alert generation
   - Verify Alert Dispatcher queries GSI correctly
   - Verify only matching subscriptions receive alerts

### Manual Test Plan

**Location:** `docs/DRUG_SHORTAGE_TESTING.md`

**Scenarios:**

1. **Trigger Standalone Shortage Alert**

   ```bash
   # 1. Upload therapeutic category config
   aws s3 cp config/shortage_monitoring/therapeutic_categories.json \
     s3://healthsignals-data-ACCOUNT/config/shortage_monitoring/

   # 2. Manually invoke openFDA fetcher
   aws lambda invoke --function-name healthsignals-openfda-fetcher \
     --payload '{}' /dev/stdout

   # 3. Verify S3 object written
   aws s3 ls s3://healthsignals-data-ACCOUNT/raw/openfda-shortages/2024/W03/

   # 4. Verify DynamoDB records
   aws dynamodb scan --table-name healthsignals-drug-shortage-state \
     --filter-expression "shortage_status = :new" \
     --expression-attribute-values '{":new":{"S":"NEW"}}'

   # 5. Check Step Functions execution
   aws stepfunctions list-executions \
     --state-machine-arn arn:aws:states:REGION:ACCOUNT:stateMachine:healthsignals-alert-generation
   ```

2. **Subscribe to Therapeutic Category**

   ```bash
   # Subscribe to Antivirals category
   curl -X PUT https://API_ID.execute-api.us-east-1.amazonaws.com/prod/preferences \
     -H "Content-Type: application/json" \
     -d '{
       "county_fips": "48143",
       "therapeutic_categories": ["Antivirals", "Antibiotics"]
     }'

   # Verify subscription updated
   curl https://API_ID.execute-api.us-east-1.amazonaws.com/prod/status?county_fips=48143
   ```

3. **Test Combined Disease + Shortage Signal**

   ```bash
   # 1. Seed shortage-state with relevant medication (Antivirals)
   aws dynamodb put-item --table-name healthsignals-drug-shortage-state \
     --item '{
       "product_id": {"S": "TEST-001"},
       "week_timestamp": {"S": "2024-W03"},
       "product_name": {"S": "Oseltamivir Capsules"},
       "therapeutic_category": {"S": "Antivirals"},
       "shortage_status": {"S": "NEW"}
     }'

   # 2. Upload disease surveillance data that crosses threshold
   # (follow existing disease outbreak testing procedure)

   # 3. Verify Pipeline Coordinator enriches with shortage context
   # Check CloudWatch Logs for "shortage_context" in payload
   ```

## Monitoring and Observability

### CloudWatch Metrics

**Namespace:** `HealthSignals/DrugShortages`

| Metric Name                            | Unit    | Description                                    |
| -------------------------------------- | ------- | ---------------------------------------------- |
| `openfda_api_success_rate`             | Percent | Percentage of successful API calls             |
| `openfda_rate_limit_errors`            | Count   | Number of 429 rate limit errors                |
| `shortage_records_fetched_count`       | Count   | Total records retrieved from openFDA           |
| `shortage_changes_detected_count`      | Count   | NEW + WORSENING + RESOLVED changes             |
| `shortage_alerts_generated_count`      | Count   | Step Functions executions started              |
| `shortage_alerts_delivered_count`      | Count   | Successful SES/SNS deliveries                  |
| `shortage_circuit_breaker_activations` | Count   | Circuit breaker triggers (>20 changes)         |
| `therapeutic_category_distribution`    | Count   | Changes per category (dimension: category_key) |

**Custom Dimensions:**

- `shortage_status`: NEW, WORSENING, RESOLVED, UNCHANGED
- `therapeutic_category`: Antivirals, Antibiotics, Respiratory, etc.
- `alert_type`: shortage, combined

### CloudWatch Alarms

| Alarm Name                  | Condition                                                | Action                                    |
| --------------------------- | -------------------------------------------------------- | ----------------------------------------- |
| `OpenFDAFetcherFailureRate` | `openfda_api_success_rate < 50%` over 2 periods (10 min) | SNS notification to ops team              |
| `ShortageAlertsDLQ`         | `shortage_alerts_dlq_message_count > 0`                  | SNS notification + PagerDuty              |
| `CircuitBreakerActivated`   | `shortage_circuit_breaker_activations > 0`               | SNS notification (manual review)          |
| `BedrockThrottling`         | `bedrock_throttling_errors > 10` over 5 min              | SNS notification + quota increase request |

### CloudWatch Dashboard Extension

**Dashboard Name:** `HealthSignals-Overview` (existing, extended)

**New Section:** Drug Shortage Intelligence

```json
{
  "widgets": [
    {
      "type": "metric",
      "properties": {
        "title": "OpenFDA API Health",
        "metrics": [
          ["HealthSignals/DrugShortages", "openfda_api_success_rate"],
          [".", "openfda_rate_limit_errors"]
        ],
        "period": 300,
        "stat": "Average",
        "region": "us-east-1"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Shortage Changes Detected",
        "metrics": [
          [
            "HealthSignals/DrugShortages",
            "shortage_changes_detected_count",
            { "stat": "Sum" }
          ]
        ],
        "period": 3600,
        "stat": "Sum",
        "region": "us-east-1"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Shortage Alerts by Type",
        "metrics": [
          [
            "HealthSignals/DrugShortages",
            "shortage_alerts_generated_count",
            { "dimensions": { "alert_type": "shortage" } }
          ],
          ["...", { "dimensions": { "alert_type": "combined" } }]
        ],
        "period": 3600,
        "stat": "Sum",
        "region": "us-east-1"
      }
    },
    {
      "type": "log",
      "properties": {
        "title": "Recent Shortage Changes",
        "query": "SOURCE '/aws/lambda/healthsignals-shortage-change-detector'\n| fields @timestamp, shortage_status, therapeutic_category, product_name\n| filter shortage_status = 'NEW' or shortage_status = 'WORSENING'\n| sort @timestamp desc\n| limit 20",
        "region": "us-east-1"
      }
    }
  ]
}
```

### X-Ray Tracing

**Trace Map Expected:**

```
EventBridge → SQS → OpenFDA Fetcher Lambda → S3 → Pipeline Coordinator
                                                  ↓
                                    Shortage Change Detector Lambda
                                                  ↓
                                          DynamoDB (state + alerts)
                                                  ↓
                                          Step Functions (parallel)
                                                  ↓
                     ┌────────────────────────────┼────────────────────────────┐
                     ▼                            ▼                            ▼
            Bedrock InvokeModel          Bedrock InvokeModel          Bedrock InvokeModel
            (Situation Brief)            (Actions)                    (Communication)
                     │                            │                            │
                     └────────────────────────────┼────────────────────────────┘
                                                  ▼
                                          Alert Dispatcher Lambda
                                                  ↓
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                            SES (Email)                   SNS (SMS)
```

**Instrumentation:**

- All Lambda functions: Automatic X-Ray tracing enabled
- Step Functions: Tracing enabled (see generation_stack.py)
- DynamoDB: Automatic service map integration
- Bedrock: Captured via AWS SDK instrumentation

**Subsegments to Add:**

- Config loading from S3
- openFDA API HTTP request
- DynamoDB queries (shortage context lookup)
- Therapeutic category filtering logic

### Structured Logging

**Log Format:** JSON (CloudWatch Logs Insights compatible)

```json
{
  "timestamp": "2024-01-15T06:10:23.456Z",
  "level": "INFO",
  "function_name": "healthsignals-shortage-change-detector",
  "trace_id": "1-5f8a5b2c-3d4e5f6a7b8c9d0e1f2a3b4c",
  "event_type": "shortage_change_detected",
  "metadata": {
    "product_id": "1234",
    "product_name": "Amoxicillin Capsules 500mg",
    "therapeutic_category": "Antibiotics",
    "shortage_status": "NEW",
    "previous_status": null,
    "week_timestamp": "2024-W03"
  }
}
```

**Key Event Types:**

- `openfda_api_request`: API call start
- `openfda_api_response`: API call complete (includes status code, latency)
- `shortage_change_detected`: Change classification result
- `circuit_breaker_evaluated`: Circuit breaker check
- `alert_generated`: Step Functions execution started
- `subscription_matched`: Subscription filter match
- `alert_delivered`: SES/SNS delivery complete

**Log Insights Queries:**

```sql
-- Count shortage changes by category in last 7 days
fields @timestamp, metadata.therapeutic_category, metadata.shortage_status
| filter event_type = "shortage_change_detected"
| stats count() by metadata.therapeutic_category, metadata.shortage_status

-- Find openFDA API errors
fields @timestamp, level, message, metadata.status_code
| filter event_type = "openfda_api_response" and metadata.status_code >= 400
| sort @timestamp desc

-- Track circuit breaker activations
fields @timestamp, metadata.change_count, metadata.new_count, metadata.worsening_count
| filter event_type = "circuit_breaker_evaluated" and metadata.activated = true
```

## Cost Estimation

### Incremental Costs (Per 100 Subscriptions with Shortage Monitoring)

| Component                            | Monthly Cost     | Notes                                      |
| ------------------------------------ | ---------------- | ------------------------------------------ |
| **Lambda (openFDA fetcher)**         | $0.50            | 4 invocations/month × 256MB × 60s          |
| **Lambda (change detector)**         | $1.20            | 4 invocations/month × 512MB × 120s         |
| **SQS (openFDA queue)**              | $0.10            | ~20 requests/month (schedule + retries)    |
| **S3 (shortage data storage)**       | $0.50            | ~2 MB/week × 52 weeks = 104 MB             |
| **DynamoDB (shortage-state)**        | $2.50            | On-demand, ~1,600 records × 4 writes/month |
| **DynamoDB (shortage-alerts)**       | $1.00            | On-demand, ~100 alert records/month        |
| **Step Functions (shortage alerts)** | $1.50            | ~50 shortage alerts/month × 3 steps        |
| **Bedrock (shortage briefs)**        | $1.90            | 50 alerts × $0.038 per alert               |
| **CloudWatch (metrics + logs)**      | $2.00            | Shortage-specific metrics + log storage    |
| **X-Ray tracing**                    | $0.50            | Incremental traces for shortage path       |
| **Total Incremental**                | **$11.70/month** | **$0.12 per subscription**                 |

### Total System Cost (Disease + Shortage Monitoring)

| Component                       | Disease Only   | + Shortage Module | Increase   |
| ------------------------------- | -------------- | ----------------- | ---------- |
| Lambda + EventBridge + SQS      | $12–20         | $14–22            | +$2        |
| S3 + DynamoDB                   | $8–15          | $12–19            | +$4        |
| Bedrock                         | $150–300       | $152–302          | +$2        |
| Step Functions                  | $3–7           | $5–9              | +$2        |
| SES + SNS + CloudWatch          | $10–20         | $12–22            | +$2        |
| **Total per 100 subscriptions** | **$184–358**   | **$195–374**      | **+$12**   |
| **Per subscription**            | **$1.84–3.58** | **$1.95–3.74**    | **+$0.12** |

**Key Insight:** Shortage monitoring adds only **6% to total cost** while providing significant value through medication availability intelligence.

## Deployment Strategy

### CDK Stack Extensions

**Stack Modification Summary:**

1. **Ingestion Stack** (`cdk/stacks/ingestion_stack.py`)
   - Add openFDA SQS queue with DLQ
   - Add openFDA fetcher Lambda
   - Add EventBridge target for weekly schedule

2. **Prediction Stack** (`cdk/stacks/prediction_stack.py`)
   - Add `healthsignals-drug-shortage-state` DynamoDB table with GSI
   - Add `healthsignals-shortage-alerts` DynamoDB table
   - Add shortage change detector Lambda

3. **Generation Stack** (`cdk/stacks/generation_stack.py`)
   - Extend Step Functions state machine with conditional branching
   - Add Bedrock IAM permissions for new prompts
   - Reference new Knowledge Base documents

4. **Orchestration Stack** (`cdk/stacks/orchestration_stack.py`)
   - Extend Pipeline Coordinator Lambda with shortage routing logic
   - Add IAM permissions for shortage-state table queries

5. **Subscription Stack** (`cdk/stacks/subscription_stack.py`)
   - Add GSI `therapeutic-category-lookup` to subscriptions table
   - Extend subscription API Lambdas for preferences updates

6. **Delivery Stack** (`cdk/stacks/delivery_stack.py`)
   - Extend Alert Dispatcher Lambda with category filtering

7. **Monitoring Stack** (`cdk/stacks/monitoring_stack.py`)
   - Add Drug Shortage Intelligence dashboard section
   - Add CloudWatch alarms for shortage monitoring

### Deployment Phases

**Phase 1: Infrastructure (Week 1)**

- Deploy extended CDK stacks
- Create DynamoDB tables with GSIs
- Deploy Lambda functions
- Configure SQS queues and EventBridge schedules

**Phase 2: Configuration (Week 2)**

- Upload therapeutic category config to S3
- Upload openFDA data source config
- Upload Knowledge Base shortage guidance documents
- Configure Bedrock Guardrails extensions

**Phase 3: Testing (Week 3)**

- Run unit tests (pytest)
- Run integration tests with mock openFDA API
- Manual testing of shortage alert generation
- Manual testing of combined disease+shortage signals
- Validate subscription API extensions

**Phase 4: Pilot (Week 4)**

- Enable shortage monitoring for 5 test counties
- Monitor CloudWatch metrics and alarms
- Collect feedback from pilot pharmacists
- Iterate on Bedrock prompts based on feedback

**Phase 5: Production (Week 5+)**

- Roll out to all existing subscriptions
- Update documentation
- Training materials for new subscribers
- Monitor cost and performance

### Rollback Plan

**Rollback Triggers:**

- Circuit breaker activates repeatedly (data quality issues)
- openFDA API reliability < 90% over 7 days
- Bedrock costs exceed budget by >20%
- Guardrails block >50% of alerts (tuning needed)

**Rollback Steps:**

1. Set `enabled: false` in `config/data_sources/openfda_shortages.json`
2. Stop EventBridge schedule for openFDA fetcher
3. Keep infrastructure in place (no stack deletion)
4. Investigate root cause
5. Re-enable after fixes

**Zero-downtime:** Disease outbreak monitoring continues unaffected during shortage module rollback.

## Scalability Considerations

### Current Design (Phase 1 MVP)

| Dimension               | Current Capacity              | Bottleneck                           | Mitigation                                                     |
| ----------------------- | ----------------------------- | ------------------------------------ | -------------------------------------------------------------- |
| **Subscriptions**       | 100                           | GSI query performance                | Add caching layer at 500+ subscriptions                        |
| **Shortage records**    | 1,638 (current openFDA total) | Lambda memory/timeout                | Already sized for full dataset (512MB, 180s)                   |
| **Concurrent alerts**   | 10-20                         | Step Functions concurrent executions | Request quota increase to 100                                  |
| **API rate limit**      | 240 req/hour                  | openFDA rate limit                   | Use pagination + respect limits                                |
| **DynamoDB throughput** | On-demand                     | Cost at scale                        | Monitor and evaluate reserved capacity at 1,000+ subscriptions |

### Scaling to 500 Subscriptions

**Required Changes:**

1. **Alert Dispatcher Optimization**
   - Add ElastiCache (Redis) for subscription lookups
   - Cache therapeutic category → subscription mappings
   - TTL: 5 minutes (balance freshness vs. hit rate)

2. **Step Functions Quotas**
   - Request increase: 100 concurrent executions (from default 25)
   - Add exponential backoff retry if throttled

3. **DynamoDB Auto-scaling**
   - Evaluate reserved capacity vs. on-demand
   - Monitor read/write patterns via CloudWatch

### Scaling to 1,000+ Subscriptions

**Required Changes:**

1. **Fan-out Pattern**
   - Replace Step Functions → SQS → Lambda fan-out
   - Batch alert deliveries (5-10 per SES call)
   - Parallel processing workers

2. **Data Lake Query Optimization**
   - Add Athena for historical shortage analysis
   - Partition S3 data by year/week for faster queries
   - Create shortage trend views for reporting

3. **Cost Optimization**
   - Reserved DynamoDB capacity for base load
   - Bedrock Provisioned Throughput if alert volume high
   - S3 Intelligent-Tiering for historical data

## Future Enhancements (Out of Scope for Phase 1)

### Phase 2 Considerations

1. **Historical Trending**
   - Track shortage duration (weeks in shortage)
   - Identify recurring shortage patterns
   - Alert when chronic shortages worsen

2. **Predictive Shortage Risk**
   - ML model: Predict shortage risk based on FDA manufacturing notices
   - Proactive alerts before shortages declared
   - Requires training data collection (6-12 months)

3. **Multi-source Data Integration**
   - ASHP Drug Shortages Database (membership required)
   - Manufacturer direct feeds (partnerships)
   - Group Purchasing Organization (GPO) data

4. **Granular Geographic Filtering**
   - State-specific shortage information
   - Regional distribution constraints
   - Local pharmacy inventory coordination

5. **Interactive Dashboard**
   - Web portal for viewing shortage trends
   - Self-service alert threshold configuration
   - Historical shortage timeline visualization

6. **Automated Therapeutic Equivalence**
   - FDA Orange Book integration
   - Therapeutic equivalence (TE) code mapping
   - Suggest alternatives WITHOUT clinical recommendations

### Migration Path to Advanced Features

**Prerequisites:**

- 6 months operational data collection
- Feedback from 100+ pharmacists
- Stable Phase 1 performance metrics
- Budget approval for ML development

**Estimated Effort:**

- Historical trending: 2 weeks
- Predictive risk: 12 weeks (requires ML expertise)
- Multi-source integration: 4-8 weeks per source
- Dashboard: 6 weeks

## References

### External Documentation

1. **openFDA Drug Shortages API**
   - API Documentation: https://open.fda.gov/apis/drug/shortages/
   - Rate Limits: https://open.fda.gov/apis/authentication/
   - Data Dictionary: https://open.fda.gov/fields/

2. **FDA Drug Shortage Management**
   - FDA Shortage Portal: https://www.accessdata.fda.gov/scripts/drugshortages/
   - Guidance Documents: https://www.fda.gov/drugs/drug-shortages/

3. **AWS Service Limits**
   - Lambda Quotas: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
   - Step Functions Quotas: https://docs.aws.amazon.com/step-functions/latest/dg/limits-overview.html
   - Bedrock Quotas: https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html

### Internal Documentation

1. **HealthSignals Architecture**
   - `/architecture/architecture.md`
   - Existing system design patterns

2. **Configuration Reference**
   - `docs/CONFIGURATION.md`
   - Config-driven scaling principles

3. **Deployment Guide**
   - `docs/DEPLOYMENT.md`
   - CDK deployment procedures

4. **Testing Documentation**
   - `docs/DRUG_SHORTAGE_TESTING.md` (to be created)
   - Manual test procedures

5. **Requirements Document**
   - `.kiro/specs/drug-shortage-intelligence/requirements.md`
   - Complete acceptance criteria

---

**Document Version:** 1.0  
**Last Updated:** 2024-01-15  
**Author:** HealthSignals Development Team  
**Status:** Design Phase Complete, Ready for Implementation
