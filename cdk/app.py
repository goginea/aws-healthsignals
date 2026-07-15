#!/usr/bin/env python3
"""Amazon HealthSignals — CDK Application Entry Point.

Deploys the complete HealthSignals Bedrock Blueprint infrastructure:
- Ingestion: EventBridge + SQS + Lambda fleet for data collection
- Prediction: Leader detection + affinity + timing estimation
- Generation: Step Functions + Bedrock integration
- Orchestration: Pipeline coordinator (S3 trigger → prediction → generation)
- Delivery: SES + SNS alert routing
- Subscription: API Gateway + Lambda for county management
- Monitoring: CloudWatch dashboards + X-Ray tracing

Optional plugin modules (controlled by context flags):
- Drug Shortage Intelligence: openFDA polling, change detection, shortage alerts
"""
import aws_cdk as cdk

from stacks.ingestion_stack import IngestionStack
from stacks.prediction_stack import PredictionStack
from stacks.generation_stack import GenerationStack
from stacks.orchestration_stack import OrchestrationStack
from stacks.delivery_stack import DeliveryStack
from stacks.subscription_stack import SubscriptionStack
from stacks.monitoring_stack import MonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-east-1",
)

# Feature flags for optional modules
# try_get_context reads from CDK CLI context (cdk synth/deploy).
# Fallback to True so the module is enabled by default when context is unavailable.
_shortage_ctx = app.node.try_get_context("enable_drug_shortage")
enable_drug_shortage = bool(_shortage_ctx) if _shortage_ctx is not None else True

# Stack deployment order matters — dependencies flow top to bottom
ingestion = IngestionStack(app, "HealthSignals-Ingestion", env=env)
prediction = PredictionStack(app, "HealthSignals-Prediction", env=env)
generation = GenerationStack(app, "HealthSignals-Generation", env=env)

# Orchestration uses bucket name string (not construct) to avoid circular dependency
orchestration = OrchestrationStack(
    app,
    "HealthSignals-Orchestration",
    data_bucket_name=ingestion.data_bucket.bucket_name,
    state_machine_arn=generation.state_machine.state_machine_arn,
    leader_detection_function_name="healthsignals-leader-detection",
    geo_affinity_function_name="healthsignals-geographic-affinity",
    timing_estimation_function_name="healthsignals-timing-estimation",
    env=env,
)

# Build plugin configuration for delivery stack
_plugin_table_arns = []
_plugin_dispatch_modules = ""
_plugin_env_vars = {}
_plugin_gsis = []

if enable_drug_shortage:
    # Shortage plugin needs access to its alerts table for delivery status tracking
    _plugin_table_arns.append(
        f"arn:aws:dynamodb:{env.region or 'us-east-1'}:*:table/healthsignals-shortage-alerts"
    )
    _plugin_dispatch_modules = "shortage_dispatch"
    _plugin_env_vars["PLUGIN_ALERTS_TABLE"] = "healthsignals-shortage-alerts"
    # Shortage plugin needs a GSI on subscriptions table for category-based lookup
    _plugin_gsis.append({
        "index_name": "alert-category-lookup",
        "partition_key": "alert_category",
        "sort_key": "county_fips",
    })

delivery = DeliveryStack(
    app,
    "HealthSignals-Delivery",
    plugin_table_arns=_plugin_table_arns,
    plugin_dispatch_modules=_plugin_dispatch_modules,
    plugin_env_vars=_plugin_env_vars,
    env=env,
)
subscription = SubscriptionStack(
    app,
    "HealthSignals-Subscription",
    plugin_gsis=_plugin_gsis,
    env=env,
)
monitoring = MonitoringStack(app, "HealthSignals-Monitoring", env=env)

# Explicit dependencies (linear chain — no cycles)
prediction.add_dependency(ingestion)
generation.add_dependency(prediction)
orchestration.add_dependency(ingestion)  # Needs bucket to exist
orchestration.add_dependency(generation)  # Needs state machine to exist
delivery.add_dependency(orchestration)
subscription.add_dependency(delivery)
monitoring.add_dependency(subscription)

# --- Optional Plugin: Drug Shortage Intelligence ---
if enable_drug_shortage:
    from stacks.shortage_stack import ShortageStack

    shortage = ShortageStack(
        app,
        "HealthSignals-DrugShortage",
        data_bucket_name=ingestion.data_bucket.bucket_name,
        ops_topic_arn=monitoring.ops_topic.topic_arn,
        env=env,
    )
    shortage.add_dependency(ingestion)   # Needs S3 bucket to exist
    shortage.add_dependency(generation)  # Needs state machine for alert generation
    shortage.add_dependency(monitoring)  # Needs ops topic for alarm actions

app.synth()
