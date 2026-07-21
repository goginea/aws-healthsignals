"""Forecast Provider Stack — Optional plugin module.

Deploys all resources for the Forecast Provider feature:
- DynamoDB forecast-state table
- FluSight fetcher Lambda + weekly schedule
- RSV Hub fetcher Lambda + weekly schedule
- Custom model fetcher Lambda
- Forecast aggregator Lambda
- CloudWatch alarms and dashboard

This stack is optional and controlled by the 'enable_forecast_providers' context flag.
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
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
)
from constructs import Construct


class ForecastProviderStack(Stack):
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
            layer_version_name="healthsignals-shared-forecast",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities for Forecast Provider Lambdas",
        )

        # --- DynamoDB: Forecast State Table ---
        self.forecast_state_table = dynamodb.Table(
            self,
            "ForecastStateTable",
            table_name="healthsignals-forecast-state",
            partition_key=dynamodb.Attribute(
                name="geo_key", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="disease_week", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # --- FluSight Fetcher Lambda ---
        self.flusight_fetcher = _lambda.Function(
            self,
            "FluSightFetcher",
            function_name="healthsignals-flusight-forecast-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/flusight_forecast_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "FORECAST_STATE_TABLE": self.forecast_state_table.table_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )
        self.forecast_state_table.grant_read_write_data(self.flusight_fetcher)
        self.flusight_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/forecasts/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )
        self.flusight_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- RSV Hub Fetcher Lambda ---
        self.rsv_hub_fetcher = _lambda.Function(
            self,
            "RSVHubFetcher",
            function_name="healthsignals-rsv-hub-forecast-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/rsv_hub_forecast_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=1024,  # Higher memory for pandas/pyarrow
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "FORECAST_STATE_TABLE": self.forecast_state_table.table_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )
        self.forecast_state_table.grant_read_write_data(self.rsv_hub_fetcher)
        self.rsv_hub_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/forecasts/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )
        self.rsv_hub_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- Custom Model Fetcher Lambda ---
        self.custom_model_fetcher = _lambda.Function(
            self,
            "CustomModelFetcher",
            function_name="healthsignals-custom-model-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/custom_model_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "FORECAST_STATE_TABLE": self.forecast_state_table.table_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )
        self.forecast_state_table.grant_read_write_data(self.custom_model_fetcher)
        self.custom_model_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/raw/forecasts/*",
                    f"arn:aws:s3:::{bucket_name}/config/*",
                ],
            )
        )
        self.custom_model_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:*"],
            )
        )
        self.custom_model_fetcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- Forecast Aggregator Lambda ---
        self.forecast_aggregator = _lambda.Function(
            self,
            "ForecastAggregator",
            function_name="healthsignals-forecast-aggregator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/forecast_aggregator"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "FORECAST_STATE_TABLE": self.forecast_state_table.table_name,
                "EVENT_BUS_NAME": "default",
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LOG_LEVEL": "INFO",
            },
        )
        self.forecast_state_table.grant_read_write_data(self.forecast_aggregator)
        self.forecast_aggregator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:event-bus/default"],
            )
        )
        self.forecast_aggregator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{bucket_name}/config/*"],
            )
        )
        self.forecast_aggregator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # --- EventBridge Schedules ---
        # FluSight: Weekly on Wednesday at 10 AM UTC
        self.flusight_schedule = events.Rule(
            self,
            "FluSightWeeklySchedule",
            schedule=events.Schedule.cron(minute="0", hour="10", week_day="WED"),
            description="Weekly FluSight ensemble forecast fetch (Wednesday 10 AM UTC)",
        )
        self.flusight_schedule.add_target(targets.LambdaFunction(self.flusight_fetcher))

        # RSV Hub: Weekly on Wednesday at 11 AM UTC
        self.rsv_hub_schedule = events.Rule(
            self,
            "RSVHubWeeklySchedule",
            schedule=events.Schedule.cron(minute="0", hour="11", week_day="WED"),
            description="Weekly RSV Hub ensemble forecast fetch (Wednesday 11 AM UTC)",
        )
        self.rsv_hub_schedule.add_target(targets.LambdaFunction(self.rsv_hub_fetcher))

        # Aggregator: Weekly on Wednesday at 12 PM UTC (after ingestion completes)
        self.aggregator_schedule = events.Rule(
            self,
            "AggregatorWeeklySchedule",
            schedule=events.Schedule.cron(minute="0", hour="12", week_day="WED"),
            description="Weekly forecast aggregation (Wednesday 12 PM UTC, after ingestion)",
        )
        self.aggregator_schedule.add_target(targets.LambdaFunction(self.forecast_aggregator))

        # --- CloudWatch Alarms ---
        ops_topic = None
        if ops_topic_arn:
            ops_topic = sns.Topic.from_topic_arn(self, "OpsTopicRef", ops_topic_arn)

        flusight_error_alarm = cw.Alarm(
            self,
            "FluSightFetcherErrorAlarm",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": "healthsignals-flusight-forecast-fetcher"},
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description="FluSight forecast fetcher Lambda errors",
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        no_data_alarm = cw.Alarm(
            self,
            "NoForecastDataAlarm",
            metric=cw.Metric(
                namespace="HealthSignals/ForecastProviders",
                metric_name="forecasts_written",
                dimensions_map={"Provider": "cdc_flusight"},
                statistic="Sum",
                period=Duration.days(7),
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description="No FluSight forecast data written in 7 days",
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
        )

        if ops_topic:
            flusight_error_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))
            no_data_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))

        # --- CloudWatch Dashboard ---
        self.dashboard = cw.Dashboard(
            self,
            "ForecastProviderDashboard",
            dashboard_name="HealthSignals-ForecastProviders",
        )

        self.dashboard.add_widgets(
            cw.TextWidget(markdown="## Forecast Providers", width=24, height=1),
        )

        self.dashboard.add_widgets(
            cw.GraphWidget(
                title="Forecasts Written by Provider",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="HealthSignals/ForecastProviders",
                        metric_name="forecasts_written",
                        dimensions_map={"Provider": "cdc_flusight"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                    cw.Metric(
                        namespace="HealthSignals/ForecastProviders",
                        metric_name="forecasts_written",
                        dimensions_map={"Provider": "cdc_rsv_hub"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
            ),
            cw.GraphWidget(
                title="Aggregation & Conflicts",
                width=12,
                height=6,
                left=[
                    cw.Metric(
                        namespace="HealthSignals/ForecastProviders",
                        metric_name="forecasts_aggregated",
                        dimensions_map={"FunctionName": "forecast_aggregator"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                    cw.Metric(
                        namespace="HealthSignals/ForecastProviders",
                        metric_name="forecast_conflicts",
                        dimensions_map={"FunctionName": "forecast_aggregator"},
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
            ),
        )
