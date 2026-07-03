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

# Stack deployment order matters — dependencies flow top to bottom
ingestion = IngestionStack(app, "HealthSignals-Ingestion", env=env)
prediction = PredictionStack(app, "HealthSignals-Prediction", env=env)
generation = GenerationStack(app, "HealthSignals-Generation", env=env)

orchestration = OrchestrationStack(
    app,
    "HealthSignals-Orchestration",
    data_bucket=ingestion.data_bucket,
    state_machine_arn=generation.state_machine.state_machine_arn,
    leader_detection_function_name="healthsignals-leader-detection",
    geo_affinity_function_name="healthsignals-geographic-affinity",
    timing_estimation_function_name="healthsignals-timing-estimation",
    env=env,
)

delivery = DeliveryStack(app, "HealthSignals-Delivery", env=env)
subscription = SubscriptionStack(app, "HealthSignals-Subscription", env=env)
monitoring = MonitoringStack(app, "HealthSignals-Monitoring", env=env)

# Explicit dependencies
prediction.add_dependency(ingestion)
generation.add_dependency(prediction)
orchestration.add_dependency(generation)
delivery.add_dependency(orchestration)
subscription.add_dependency(delivery)
monitoring.add_dependency(subscription)

app.synth()
