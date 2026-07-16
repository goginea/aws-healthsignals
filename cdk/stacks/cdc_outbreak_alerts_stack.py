"""CDC Outbreak Alerts Stack — Optional plugin module.

Deploys all resources for the CDC Outbreak Alerts feature:
- DynamoDB table (cdc-outbreak-state)
- CDC Outbreak Fetcher Lambda + EventBridge daily schedule
- Outbreak Processor Lambda
- Step Functions state machine (outbreak alert generation)
- CloudWatch alarms and dashboard

This stack is optional and controlled by the 'enable_cdc_outbreak_alerts' context flag.
It accepts shared infrastructure references (S3 bucket, ops topic) from core stacks.
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
)
from constructs import Construct


class CDCOutbreakAlertsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_bucket_name: str,
        ops_topic_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket_name = data_bucket_name or f"healthsignals-data-{self.account}-{self.region}"

        # --- Shared Lambda Layer ---
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-cdc-outbreaks",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities for CDC Outbreak Alerts Lambdas",
        )

        # --- Step Functions: Outbreak Alert Generation ---
        self.bedrock_role = iam.Role(
            self,
            "OutbreakBedrockRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Allows CDC Outbreak Step Functions to invoke Bedrock and Lambda",
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
            "OutbreakAlertGeneration",
            state_machine_name="healthsignals-outbreak-alert-generation",
            definition_body=sfn.DefinitionBody.from_file(
                "../stepfunctions/outbreak_alert_generation.asl.json"
            ),
            role=self.bedrock_role,
            timeout=Duration.minutes(10),
            tracing_enabled=True,
        )

        # --- DynamoDB: CDC Outbreak State Table ---
        self.outbreak_state_table = dynamodb.Table(
            self,
            "OutbreakStateTable",
            table_name="healthsignals-cdc-outbreak-state",
            partition_key=dynamodb.Attribute(
                name="outbreak_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # --- CDC Outbreak Fetcher Lambda ---
        self.cdc_outbreak_fetcher = _lambda.Function(
            self,
            "CDCOutbreakFetcher",
            function_name="healthsignals-cdc-outbreak-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/cdc_outbreak_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "OUTBREAK_STATE_TABLE": self.outbreak_state_table.table_name,
                "OUTBREAK_PROCESSOR_FUNCTION": "healthsignals-outbreak-processor",
                "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "LOG_LEVEL": "INFO",
            },
        )

        # DynamoDB read/write for state tracking
        self.outbreak_state_table.grant_read_write_data(self.cdc_outbreak_fetcher)

        # S3 write for storing parsed outbreak data
        self.cdc_outbreak_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/cdc-outbreaks/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )

        # Bedrock invoke for content extraction
        self.cdc_outbreak_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Lambda invoke for outbreak processor
        self.cdc_outbreak_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:healthsignals-outbreak-processor",
                ],
            )
        )

        # CloudWatch metrics
        self.cdc_outbreak_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- Outbreak Processor Lambda ---
        self.outbreak_processor = _lambda.Function(
            self,
            "OutbreakProcessor",
            function_name="healthsignals-outbreak-processor",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/orchestration/outbreak_processor"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(3),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "STATE_MACHINE_ARN": self.state_machine.state_machine_arn,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )

        # S3 read for config
        self.outbreak_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{bucket_name}/config/*"],
            )
        )

        # Step Functions start execution
        self.outbreak_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.state_machine.state_machine_arn],
            )
        )

        # --- EventBridge Schedule: Daily RSS polling ---
        self.daily_schedule = events.Rule(
            self,
            "DailyOutbreakFetchSchedule",
            schedule=events.Schedule.cron(
                minute="0", hour="8"
            ),
            description="Triggers daily CDC Outbreaks RSS fetch at 8 AM UTC",
        )
        self.daily_schedule.add_target(
            targets.LambdaFunction(self.cdc_outbreak_fetcher)
        )

        # --- CloudWatch Alarms ---
        ops_topic = None
        if ops_topic_arn:
            ops_topic = sns.Topic.from_topic_arn(self, "OpsTopicRef", ops_topic_arn)

        # Alarm: Fetcher failures
        fetcher_error_alarm = cw.Alarm(
            self,
            "FetcherErrorAlarm",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": "healthsignals-cdc-outbreak-fetcher"},
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description="CDC Outbreak Fetcher Lambda errors — RSS fetch or Bedrock extraction may be failing.",
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # Alarm: No outbreaks fetched in 7 days (RSS feed may be unreachable)
        no_data_alarm = cw.Alarm(
            self,
            "NoDataAlarm",
            metric=cw.Metric(
                namespace="HealthSignals/CDCOutbreaks",
                metric_name="outbreaks_in_rss_feed",
                dimensions_map={"FunctionName": "cdc_outbreak_fetcher"},
                statistic="Sum",
                period=Duration.days(7),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description="No CDC outbreak data fetched in 7 days — RSS feed may be unreachable or empty.",
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
        )

        if ops_topic:
            fetcher_error_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))
            no_data_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))

        # --- CloudWatch Dashboard ---
        self.dashboard = cw.Dashboard(
            self,
            "OutbreakDashboard",
            dashboard_name="HealthSignals-CDCOutbreaks",
        )

        self.dashboard.add_widgets(
            cw.TextWidget(
                markdown="## CDC Outbreak Alerts",
                width=24,
                height=1,
            ),
        )

        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Outbreaks Detected",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="HealthSignals/CDCOutbreaks",
                        metric_name="new_outbreaks_detected",
                        dimensions_map={"FunctionName": "cdc_outbreak_fetcher"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                    cw.Metric(
                        namespace="HealthSignals/CDCOutbreaks",
                        metric_name="updated_outbreaks_detected",
                        dimensions_map={"FunctionName": "cdc_outbreak_fetcher"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
            ),
            cw.GraphWidget(
                title="Fetcher Lambda Errors",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": "healthsignals-cdc-outbreak-fetcher"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": "healthsignals-outbreak-processor"},
                        statistic="Sum",
                        period=Duration.hours(1),
                    ),
                ],
            ),
        )
