# Adding New Modules — Plugin Development Guide

HealthSignals uses a plugin architecture that allows new modules to be added without modifying core code. Each module is a self-contained add-on with its own infrastructure, business logic, and monitoring.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                    cdk/app.py (Composition Root)           │
│                                                           │
│  Feature flags → Plugin config → Stack instantiation      │
└──────────────────────┬────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────────────────┐
        │              │                          │
        ▼              ▼                          ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐
│ Core Stacks  │ │ DrugShortage │ │ Future Module Stack  │
│ (7 stacks)   │ │ Stack        │ │                      │
│              │ │              │ │                      │
│ Always       │ │ Optional     │ │ Optional             │
│ deployed     │ │ (flag-based) │ │ (flag-based)         │
└──────────────┘ └──────────────┘ └──────────────────────┘
```

---

## What a Plugin Module Owns

Each module is responsible for its own:

| Concern | Module owns | Core provides |
|---------|------------|---------------|
| Data ingestion | Fetcher Lambda + SQS + EventBridge schedule | S3 data bucket (shared) |
| Processing | Change detection / analysis Lambda | EventBridge events to subscribe to |
| Alert generation | Own Step Functions state machine + Bedrock prompts | — |
| Alert delivery | Dispatch plugin module (registered at runtime) | Alert dispatcher registry infrastructure |
| Monitoring | Own CloudWatch alarms + dashboard | Ops SNS topic (shared) |
| DynamoDB tables | Own tables in own CDK stack | — |
| Subscriptions | Adds GSI to shared subscriptions table | Subscriptions table + update_preferences API |
| Config | Own config files in `config/` | config_loader shared utility |

---

## Extension Points (No Core Changes Needed)

| Extension Point | Mechanism | Location |
|----------------|-----------|----------|
| **EventBridge events** | Core emits `healthsignals.disease.threshold_crossed` on detection | Pipeline coordinator |
| **Dispatch registry** | `DISPATCH_PLUGINS` env var loads plugin modules with `register()` | Alert dispatcher |
| **Plugin env vars** | `plugin_env_vars` dict merged into dispatcher Lambda env | DeliveryStack CDK |
| **Plugin table IAM** | `plugin_table_arns` list grants DynamoDB access | DeliveryStack CDK |
| **Plugin GSIs** | `plugin_gsis` list adds indexes to subscriptions table | SubscriptionStack CDK |
| **Feature flags** | `cdk.json` context keys control stack instantiation | app.py |
| **S3 data prefixes** | Each module writes to its own S3 prefix | Shared data bucket |
| **Config files** | Each module reads its own config paths | Shared config_loader |

---

## Step-by-Step: Adding a New Module (Example: Vaccine Supply)

### Step 1: Create the Feature Flag

In `cdk/cdk.json`:

```json
{
  "context": {
    "enable_vaccine_supply": true
  }
}
```

### Step 2: Create the CDK Stack

Create `cdk/stacks/vaccine_supply_stack.py`:

```python
from aws_cdk import Stack, Duration, RemovalPolicy, ...
from constructs import Construct

class VaccineSupplyStack(Stack):
    def __init__(self, scope, construct_id, data_bucket_name, ops_topic_arn="", **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # Own DynamoDB tables
        self.vaccine_state_table = dynamodb.Table(...)

        # Own Lambdas
        self.fetcher = _lambda.Function(...)
        self.change_detector = _lambda.Function(...)
        self.enrichment = _lambda.Function(...)

        # Own Step Functions state machine
        self.state_machine = sfn.StateMachine(...)

        # Own EventBridge rules
        self.weekly_rule = events.Rule(...)
        self.disease_threshold_rule = events.Rule(
            event_pattern=events.EventPattern(
                source=["healthsignals.pipeline_coordinator"],
                detail_type=["healthsignals.disease.threshold_crossed"],
            ),
        )

        # Own CloudWatch alarms and dashboard
        ...
```

### Step 3: Create the Fetcher Lambda

Create `lambdas/ingestion/vaccine_supply_fetcher/handler.py`:

```python
"""Vaccine Supply Fetcher — polls external vaccine supply API."""

def lambda_handler(event, context):
    # Fetch data from vaccine supply API
    # Store in S3: raw/vaccine-supply/{year}/W{week}/data.json
    ...
```

### Step 4: Create the Dispatch Plugin

Create `lambdas/delivery/alert_dispatcher/vaccine_dispatch.py`:

```python
"""Vaccine Supply dispatch plugin for alert_dispatcher registry."""

def register(context):
    """Register handlers for vaccine supply alert types."""
    # context provides: sub_table, ses, sns, system, api_base_url, dynamodb
    global _sub_table, _ses, ...
    _sub_table = context["sub_table"]
    ...

    return {
        "vaccine_supply": dispatch_vaccine_alert,
        "vaccine_combined": dispatch_vaccine_alert,
    }

def dispatch_vaccine_alert(event, alert_type):
    """Deliver vaccine supply alerts to subscribers."""
    ...
```

### Step 5: Create the Step Functions ASL

Create `stepfunctions/vaccine_alert_generation.asl.json`:

```json
{
  "Comment": "Vaccine Supply — Alert Generation",
  "StartAt": "GenerateBrief",
  "States": {
    "GenerateBrief": {
      "Type": "Task",
      "Resource": "arn:aws:states:::bedrock:invokeModel",
      ...
      "Next": "DispatchAlert"
    },
    "DispatchAlert": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "healthsignals-alert-dispatcher",
        "Payload.$": "$"
      },
      ...
    }
  }
}
```

### Step 6: Register in app.py

```python
# Feature flag
enable_vaccine_supply = bool(app.node.try_get_context("enable_vaccine_supply") or False)

# Plugin config for delivery/subscription stacks
if enable_vaccine_supply:
    _plugin_table_arns.append(
        f"arn:aws:dynamodb:{env.region}:*:table/healthsignals-vaccine-supply-state"
    )
    _plugin_dispatch_modules += ",vaccine_dispatch" if _plugin_dispatch_modules else "vaccine_dispatch"
    _plugin_env_vars["VACCINE_STATE_TABLE"] = "healthsignals-vaccine-supply-state"
    _plugin_gsis.append({
        "index_name": "vaccine-category-lookup",
        "partition_key": "vaccine_category",
        "sort_key": "county_fips",
    })

# Instantiate stack (conditional import)
if enable_vaccine_supply:
    from stacks.vaccine_supply_stack import VaccineSupplyStack

    vaccine = VaccineSupplyStack(
        app,
        "HealthSignals-VaccineSupply",
        data_bucket_name=ingestion.data_bucket.bucket_name,
        ops_topic_arn=monitoring.ops_topic.topic_arn,
        env=env,
    )
    vaccine.add_dependency(ingestion)
    vaccine.add_dependency(generation)
    vaccine.add_dependency(monitoring)
```

### Step 7: Add Config Files

Create `config/vaccine_monitoring/categories.json` and add entries to `config/alert_categories.json`:

```json
{
  "category_key": "flu_vaccines",
  "display_name": "Influenza Vaccines",
  "module": "vaccine_supply",
  "relevant_diseases": ["influenza"],
  "priority_level": "HIGH"
}
```

### Step 8: Deploy

```bash
npx aws-cdk deploy --all
aws s3 sync config/ s3://${CONFIG_BUCKET}/config/
```

---

## Core Changes Required

For any new module following this pattern, the only core file that changes is **`cdk/app.py`** (~10 lines):
- Read feature flag
- Append plugin table ARN, dispatch module name, env vars, and GSI to the lists
- Conditionally import and instantiate the stack

No Lambda code, no CDK stack definitions, no Step Functions ASL in core are modified.

---

## Design Principles

1. **One-directional dependency**: Plugin → Core, never Core → Plugin
2. **Event-driven communication**: Core emits events; plugins subscribe
3. **Registry pattern for dispatch**: Plugins register their handlers; core routes by alert_type
4. **Own infrastructure**: Each module owns its tables, Lambdas, state machine, alarms
5. **Shared delivery endpoint**: All modules converge at the alert dispatcher for final delivery
6. **Feature flags for lifecycle**: Enable, disable, or remove a module without touching other code

---

## Checklist for a New Module

- [ ] Feature flag in `cdk/cdk.json`
- [ ] CDK stack in `cdk/stacks/`
- [ ] Fetcher Lambda in `lambdas/ingestion/`
- [ ] Processing Lambda in `lambdas/prediction/` or `lambdas/orchestration/`
- [ ] Enrichment Lambda (if combining with disease alerts) in `lambdas/orchestration/`
- [ ] Dispatch plugin in `lambdas/delivery/alert_dispatcher/`
- [ ] Step Functions ASL in `stepfunctions/`
- [ ] Config files in `config/`
- [ ] Alert categories registered in `config/alert_categories.json`
- [ ] Registration in `cdk/app.py` (feature flag + plugin config + stack)
- [ ] Unit tests in `tests/unit/`
- [ ] Integration tests in `tests/integration/`
- [ ] Documentation in `docs/`
