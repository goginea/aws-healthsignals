"""Monitoring Stack — CloudWatch dashboards + alarms + X-Ray tracing.

Deploys:
- CloudWatch dashboard with key metrics (ingestion, prediction, generation, delivery, DLQ)
- Alarms for ingestion failures, SFN failures, delivery failures, DLQ depth
- SNS topic + email subscription for ops notifications
"""
from aws_cdk import (
    Stack,
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct


class MonitoringStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Ops Alert Topic ---
        self.ops_topic = sns.Topic(
            self,
            "OpsAlertTopic",
            topic_name="healthsignals-ops-alerts",
            display_name="HealthSignals Ops",
        )

        # SNS Email Subscription (ops team gets alarm notifications)
        ops_email = self.node.try_get_context("ops_email")
        if ops_email:
            self.ops_topic.add_subscription(
                subs.EmailSubscription(ops_email)
            )

        # --- CloudWatch Dashboard ---
        self.dashboard = cw.Dashboard(
            self,
            "HealthSignalsDashboard",
            dashboard_name="HealthSignals-Operations",
        )

        # Title Widget
        self.dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Amazon HealthSignals — Operations Dashboard",
                width=24,
                height=1,
            ),
        )

        # Widget: Lambda Invocations & Errors
        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Ingestion Lambda Invocations",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": "HealthSignals-Ingestion-DelphiFetcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": "HealthSignals-Ingestion-CDCWastewaterFetcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": "HealthSignals-Ingestion-CDCRespiratoryFetcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
            cw.GraphWidget(
                title="Prediction Pipeline Errors",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": "HealthSignals-Prediction-LeaderDetection"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": "healthsignals-pipeline-coordinator"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
        )

        # Widget: Step Functions Execution Metrics
        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Alert Generation Executions",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsSucceeded",
                        dimensions_map={"StateMachineArn": "healthsignals-alert-generation"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                    cw.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsFailed",
                        dimensions_map={"StateMachineArn": "healthsignals-alert-generation"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
            ),
            cw.SingleValueWidget(
                title="Bedrock Token Usage (est.)",
                width=12,
                height=6,
                metrics=[
                    cw.Metric(
                        namespace="AWS/Bedrock",
                        metric_name="InputTokenCount",
                        statistic="Sum",
                        period=Duration.days(7),
                    ),
                ],
            ),
        )

        # Widget: DLQ Depth (critical — messages here mean ingestion failures)
        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Dead Letter Queue Depth (Ingestion Failures)",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/SQS",
                        metric_name="ApproximateNumberOfMessagesVisible",
                        dimensions_map={"QueueName": "healthsignals-ingestion-dlq"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                    ),
                ],
            ),
            cw.GraphWidget(
                title="Alert Delivery Errors",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": "HealthSignals-Delivery-AlertDispatcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
        )

        # --- ALARMS ---

        # Alarm 1: Ingestion DLQ has messages (data pipeline failures)
        dlq_alarm = cw.Alarm(
            self,
            "DLQDepthAlarm",
            metric=cw.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": "healthsignals-ingestion-dlq"},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Messages in ingestion DLQ — data fetch failed after 3 retries. "
                "Check Delphi API status or CDC Socrata availability."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Alarm 2: Ingestion Lambda errors (any fetcher failing)
        ingestion_error_alarm = cw.Alarm(
            self,
            "IngestionErrorAlarm",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": "HealthSignals-Ingestion-DelphiFetcher"},
                statistic="Sum",
                period=Duration.days(1),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Delphi fetcher Lambda has errors — surveillance data may be stale. "
                "Check API connectivity and response format."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        ingestion_error_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Alarm 3: Step Functions execution failures
        sfn_failure_alarm = cw.Alarm(
            self,
            "SFNFailureAlarm",
            metric=cw.Metric(
                namespace="AWS/States",
                metric_name="ExecutionsFailed",
                dimensions_map={"StateMachineArn": "healthsignals-alert-generation"},
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Alert generation Step Functions execution failed. "
                "Counties may not receive their alerts. Check Bedrock model access and throttling."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        sfn_failure_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Alarm 4: Delivery Lambda errors (alerts not reaching counties)
        delivery_error_alarm = cw.Alarm(
            self,
            "DeliveryErrorAlarm",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": "HealthSignals-Delivery-AlertDispatcher"},
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Alert dispatcher has errors — health officers may not be receiving alerts. "
                "Check SES/SNS delivery status and subscription table access."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        delivery_error_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # --- DRUG SHORTAGE INTELLIGENCE ALARMS ---

        # Alarm 5: openFDA Fetcher API failure rate
        openfda_failure_alarm = cw.Alarm(
            self,
            "OpenFDAFetcherFailureAlarm",
            metric=cw.Metric(
                namespace="HealthSignals/DrugShortages",
                metric_name="api_success_rate",
                dimensions_map={"FunctionName": "openfda_shortage_fetcher"},
                statistic="Average",
                period=Duration.minutes(5),
            ),
            threshold=0.5,
            evaluation_periods=2,
            alarm_description=(
                "openFDA API success rate below 50% over 2 periods. "
                "Check FDA API status and network connectivity."
            ),
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        openfda_failure_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Alarm 6: Circuit breaker activated
        circuit_breaker_alarm = cw.Alarm(
            self,
            "ShortageCircuitBreakerAlarm",
            metric=cw.Metric(
                namespace="HealthSignals/DrugShortages",
                metric_name="shortage_circuit_breaker_activations",
                dimensions_map={"FunctionName": "shortage_change_detector"},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Drug shortage circuit breaker activated — >20 NEW/WORSENING shortages detected. "
                "Manual review required before alerts are generated."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        circuit_breaker_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # Alarm 7: Shortage alerts DLQ depth (openFDA queue failures)
        shortage_dlq_alarm = cw.Alarm(
            self,
            "ShortageDLQAlarm",
            metric=cw.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": "healthsignals-ingestion-dlq"},
                statistic="Maximum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description=(
                "Shortage ingestion messages in DLQ — openFDA fetch failed after 3 retries."
            ),
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        shortage_dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_topic))

        # --- Drug Shortage Intelligence Section ---
        self.dashboard.add_widgets(
            cw.TextWidget(
                markdown="## Drug Shortage Intelligence",
                width=24,
                height=1,
            ),
        )

        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="OpenFDA API Health",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="api_success_rate",
                        dimensions_map={"FunctionName": "openfda_shortage_fetcher"},
                        statistic="Average",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="records_fetched_count",
                        dimensions_map={"FunctionName": "openfda_shortage_fetcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
            cw.GraphWidget(
                title="Shortage Changes Detected",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="shortage_changes_detected_count",
                        dimensions_map={"FunctionName": "shortage_change_detector"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="shortage_alerts_generated_count",
                        dimensions_map={"FunctionName": "shortage_change_detector"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
        )

        self.dashboard.add_widgets(
            cw.SingleValueWidget(
                title="Circuit Breaker Activations (7d)",
                width=8,
                height=4,
                metrics=[
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="shortage_circuit_breaker_activations",
                        dimensions_map={"FunctionName": "shortage_change_detector"},
                        statistic="Sum",
                        period=Duration.days(7),
                    ),
                ],
            ),
            cw.SingleValueWidget(
                title="Shortage Alerts Delivered (7d)",
                width=8,
                height=4,
                metrics=[
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="shortage_alerts_generated_count",
                        dimensions_map={"FunctionName": "shortage_change_detector"},
                        statistic="Sum",
                        period=Duration.days(7),
                    ),
                ],
            ),
            cw.SingleValueWidget(
                title="Records Fetched (last run)",
                width=8,
                height=4,
                metrics=[
                    cw.Metric(
                        namespace="HealthSignals/DrugShortages",
                        metric_name="records_fetched_count",
                        dimensions_map={"FunctionName": "openfda_shortage_fetcher"},
                        statistic="Maximum",
                        period=Duration.days(7),
                    ),
                ],
            ),
        )
