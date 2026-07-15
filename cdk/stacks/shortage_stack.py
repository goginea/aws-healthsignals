"""Drug Shortage Intelligence Stack — Optional plugin module.

Deploys all resources for the Drug Shortage Intelligence feature:
- DynamoDB tables (shortage-state, shortage-alerts)
- openFDA Fetcher Lambda + SQS queue + DLQ integration
- Shortage Change Detector Lambda
- EventBridge schedule (weekly polling)
- CloudWatch alarms and dashboard widgets for shortage monitoring

This stack is optional and controlled by the 'enable_drug_shortage' context flag.
It accepts shared infrastructure references (S3 bucket, Lambda layer, ops topic)
from core stacks to maintain one-directional coupling.
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda_event_sources as lambda_event_sources,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
)
from constructs import Construct


class ShortageStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_bucket_name: str,
        shared_layer_arn: str = "",
        ops_topic_arn: str = "",
        ingestion_dlq_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket_name = data_bucket_name or f"healthsignals-data-{self.account}-{self.region}"

        # --- Shared Lambda Layer ---
        # Create a shortage-specific layer (same source, avoids cross-stack layer issues)
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-shortage",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities for Drug Shortage Lambdas: config_loader, token_utils",
        )

        # --- Step Functions: Shortage Alert Generation ---
        self.bedrock_role = iam.Role(
            self,
            "ShortageBedrockRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Allows Shortage Step Functions to invoke Bedrock models and Lambda",
        )
        self.bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                ],
            )
        )
        self.bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:healthsignals-alert-dispatcher",
                ],
            )
        )

        self.state_machine = sfn.StateMachine(
            self,
            "ShortageAlertGeneration",
            state_machine_name="healthsignals-shortage-alert-generation",
            definition_body=sfn.DefinitionBody.from_file(
                "../stepfunctions/shortage_alert_generation.asl.json"
            ),
            role=self.bedrock_role,
            timeout=Duration.minutes(10),
            tracing_enabled=True,
        )

        # --- Dead Letter Queue (shortage-specific) ---
        self.shortage_dlq = sqs.Queue(
            self,
            "ShortageDLQ",
            queue_name="healthsignals-shortage-dlq",
            retention_period=Duration.days(14),
            visibility_timeout=Duration.minutes(1),
        )

        # --- DynamoDB: Drug Shortage State Table ---
        self.shortage_state_table = dynamodb.Table(
            self,
            "ShortageStateTable",
            table_name="healthsignals-drug-shortage-state",
            partition_key=dynamodb.Attribute(
                name="product_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="week_timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        self.shortage_state_table.add_global_secondary_index(
            index_name="therapeutic-category-index",
            partition_key=dynamodb.Attribute(
                name="therapeutic_category", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="week_timestamp", type=dynamodb.AttributeType.STRING
            ),
        )

        # --- DynamoDB: Shortage Alerts (Idempotency) Table ---
        self.shortage_alerts_table = dynamodb.Table(
            self,
            "ShortageAlertsTable",
            table_name="healthsignals-shortage-alerts",
            partition_key=dynamodb.Attribute(
                name="product_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="week_timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- SQS: OpenFDA Shortages Queue ---
        self.openfda_queue = sqs.Queue(
            self,
            "OpenFDAShortagesQueue",
            queue_name="healthsignals-openfda-shortages-queue",
            visibility_timeout=Duration.minutes(6),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.shortage_dlq,
            ),
        )

        # --- OpenFDA Drug Shortage Fetcher Lambda ---
        self.openfda_fetcher = _lambda.Function(
            self,
            "OpenFDAShortagesFetcher",
            function_name="healthsignals-openfda-shortage-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/openfda_shortage_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
            },
        )

        # S3 read/write for storing fetched data
        self.openfda_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/openfda-shortages/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )

        # CloudWatch metrics permission
        self.openfda_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # SQS triggers fetcher Lambda
        self.openfda_fetcher.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.openfda_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # --- Shortage Change Detector Lambda ---
        self.shortage_change_detector = _lambda.Function(
            self,
            "ShortageChangeDetector",
            function_name="healthsignals-shortage-change-detector",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/shortage_change_detector"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(3),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "SHORTAGE_STATE_TABLE": self.shortage_state_table.table_name,
                "SHORTAGE_ALERTS_TABLE": self.shortage_alerts_table.table_name,
                "STATE_MACHINE_ARN": self.state_machine.state_machine_arn,
            },
        )
        self.shortage_state_table.grant_read_write_data(self.shortage_change_detector)
        self.shortage_alerts_table.grant_read_write_data(self.shortage_change_detector)

        # S3 read for raw shortage data and config
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/openfda-shortages/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )

        # Step Functions invoke permission
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.state_machine.state_machine_arn],
            )
        )

        # CloudWatch PutMetricData permission
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- S3 Event Notification: Trigger change detector on new openFDA data ---
        data_bucket = s3.Bucket.from_bucket_name(self, "DataBucketRef", bucket_name)
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.shortage_change_detector),
            s3.NotificationKeyFilter(prefix="raw/openfda-shortages/", suffix=".json"),
        )

        # --- EventBridge Schedule: Weekly openFDA polling ---
        self.weekly_shortage_rule = events.Rule(
            self,
            "WeeklyShortageSchedule",
            schedule=events.Schedule.cron(
                minute="0", hour="6", week_day="MON"
            ),
            description="Triggers weekly openFDA drug shortage data ingestion via SQS",
        )
        self.weekly_shortage_rule.add_target(
            targets.SqsQueue(
                self.openfda_queue,
                message=events.RuleTargetInput.from_object(
                    {"source": "scheduled", "trigger": "weekly_monday"}
                ),
            )
        )

        # --- Shortage Enrichment Lambda (combined disease + shortage alerts) ---
        self.shortage_enrichment = _lambda.Function(
            self,
            "ShortageEnrichment",
            function_name="healthsignals-shortage-enrichment",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/orchestration/shortage_enrichment"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(3),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "SHORTAGE_STATE_TABLE": self.shortage_state_table.table_name,
                "STATE_MACHINE_ARN": self.state_machine.state_machine_arn,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )

        # Read shortage-state table (GSI query for therapeutic categories)
        self.shortage_state_table.grant_read_data(self.shortage_enrichment)

        # S3 read for config files (therapeutic categories)
        self.shortage_enrichment.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{bucket_name}/config/*"],
            )
        )

        # Step Functions start execution (for combined alerts)
        self.shortage_enrichment.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.state_machine.state_machine_arn],
            )
        )

        # --- EventBridge Rule: Subscribe to disease threshold events ---
        self.disease_threshold_rule = events.Rule(
            self,
            "DiseaseThresholdRule",
            event_pattern=events.EventPattern(
                source=["healthsignals.pipeline_coordinator"],
                detail_type=["healthsignals.disease.threshold_crossed"],
            ),
            description="Routes disease threshold events to shortage enrichment Lambda",
        )
        self.disease_threshold_rule.add_target(
            targets.LambdaFunction(self.shortage_enrichment)
        )

        # --- CloudWatch Alarms ---
        ops_topic = None
        if ops_topic_arn:
            ops_topic = sns.Topic.from_topic_arn(self, "OpsTopicRef", ops_topic_arn)

        # Alarm: openFDA Fetcher API failure rate
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

        # Alarm: Circuit breaker activated
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

        # Alarm: Shortage DLQ depth
        shortage_dlq_alarm = cw.Alarm(
            self,
            "ShortageDLQAlarm",
            metric=cw.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": self.shortage_dlq.queue_name},
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

        if ops_topic:
            openfda_failure_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))
            circuit_breaker_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))
            shortage_dlq_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))

        # --- CloudWatch Dashboard ---
        self.dashboard = cw.Dashboard(
            self,
            "ShortageDashboard",
            dashboard_name="HealthSignals-DrugShortage",
        )

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
