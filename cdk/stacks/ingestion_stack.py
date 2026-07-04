"""Ingestion Stack — EventBridge Scheduler + SQS + Lambda data collection fleet.

Deploys:
- S3 bucket for raw surveillance data (time-partitioned)
- SQS queues with DLQ for ingestion backpressure and failure recovery
- Three Lambda functions: Delphi fetcher, CDC wastewater, CDC respiratory
- EventBridge Scheduler rules (weekly cron → SQS → Lambda)
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_lambda as _lambda,
    aws_sqs as sqs,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda_event_sources as lambda_event_sources,
    aws_iam as iam,
)
from constructs import Construct


class IngestionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- S3 Data Lake ---
        self.data_bucket = s3.Bucket(
            self,
            "SurveillanceDataLake",
            bucket_name=f"healthsignals-data-{self.account}-{self.region}",
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ArchiveOldData",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(90),
                        )
                    ],
                )
            ],
        )

        # --- Dead Letter Queue (shared) ---
        # Failed ingestion messages land here for investigation.
        # CloudWatch alarm should fire if messages appear in DLQ.
        self.ingestion_dlq = sqs.Queue(
            self,
            "IngestionDLQ",
            queue_name="healthsignals-ingestion-dlq",
            retention_period=Duration.days(14),
            visibility_timeout=Duration.minutes(1),
        )

        # --- SQS Queues (one per data source for independent backpressure) ---
        self.delphi_queue = sqs.Queue(
            self,
            "DelphiIngestionQueue",
            queue_name="healthsignals-ingest-delphi",
            visibility_timeout=Duration.minutes(6),  # > Lambda timeout
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,  # 3 retries before DLQ
                queue=self.ingestion_dlq,
            ),
        )

        self.wastewater_queue = sqs.Queue(
            self,
            "WastewaterIngestionQueue",
            queue_name="healthsignals-ingest-wastewater",
            visibility_timeout=Duration.minutes(6),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.ingestion_dlq,
            ),
        )

        self.respiratory_queue = sqs.Queue(
            self,
            "RespiratoryIngestionQueue",
            queue_name="healthsignals-ingest-respiratory",
            visibility_timeout=Duration.minutes(6),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.ingestion_dlq,
            ),
        )

        # --- Shared Lambda Layer (config_loader + utilities) ---
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-utils",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description=(
                "Shared utilities for HealthSignals Lambdas: "
                "config_loader, token_utils, date helpers"
            ),
        )

        # --- Delphi Epidata Fetcher ---
        self.delphi_fetcher = _lambda.Function(
            self,
            "DelphiFetcher",
            function_name="healthsignals-delphi-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/delphi_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_PREFIX": "config/",
                "DELPHI_API_BASE": "https://api.delphi.cmu.edu/epidata/covidcast/",
            },
        )
        self.data_bucket.grant_read_write(self.delphi_fetcher)
        # SQS triggers Lambda (with batch size 1 — each message = one full ingestion run)
        self.delphi_fetcher.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.delphi_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # --- CDC Wastewater Fetcher ---
        self.cdc_wastewater_fetcher = _lambda.Function(
            self,
            "CDCWastewaterFetcher",
            function_name="healthsignals-cdc-wastewater-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/cdc_wastewater_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_PREFIX": "config/",
            },
        )
        self.data_bucket.grant_read_write(self.cdc_wastewater_fetcher)
        self.cdc_wastewater_fetcher.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.wastewater_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # --- CDC Respiratory Activity Fetcher ---
        self.cdc_respiratory_fetcher = _lambda.Function(
            self,
            "CDCRespiratoryFetcher",
            function_name="healthsignals-cdc-respiratory-fetcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/ingestion/cdc_respiratory_fetcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_PREFIX": "config/",
            },
        )
        self.data_bucket.grant_read_write(self.cdc_respiratory_fetcher)
        self.cdc_respiratory_fetcher.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.respiratory_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # --- OpenFDA Drug Shortages Queue ---
        self.openfda_queue = sqs.Queue(
            self,
            "OpenFDAShortagesQueue",
            queue_name="healthsignals-openfda-shortages-queue",
            visibility_timeout=Duration.minutes(6),  # > Lambda timeout
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.ingestion_dlq,
            ),
        )

        # --- OpenFDA Drug Shortage Fetcher ---
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
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_BUCKET": self.data_bucket.bucket_name,
                "CONFIG_PREFIX": "config/",
            },
        )
        self.data_bucket.grant_read_write(self.openfda_fetcher)
        self.openfda_fetcher.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.openfda_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
            )
        )

        # --- EventBridge Scheduler: Weekly Monday 6 AM UTC ---
        # EventBridge sends messages to SQS (not directly to Lambda).
        # This decouples scheduling from execution — if a fetcher fails,
        # SQS retries up to 3 times before sending to DLQ.
        weekly_rule = events.Rule(
            self,
            "WeeklyIngestionSchedule",
            schedule=events.Schedule.cron(
                minute="0", hour="6", week_day="MON"
            ),
            description="Triggers weekly surveillance data ingestion via SQS",
        )
        weekly_rule.add_target(
            targets.SqsQueue(
                self.delphi_queue,
                message=events.RuleTargetInput.from_object(
                    {"source": "scheduled", "trigger": "weekly_monday"}
                ),
            )
        )
        weekly_rule.add_target(
            targets.SqsQueue(
                self.wastewater_queue,
                message=events.RuleTargetInput.from_object(
                    {"source": "scheduled", "trigger": "weekly_monday"}
                ),
            )
        )
        weekly_rule.add_target(
            targets.SqsQueue(
                self.respiratory_queue,
                message=events.RuleTargetInput.from_object(
                    {"source": "scheduled", "trigger": "weekly_monday"}
                ),
            )
        )
        weekly_rule.add_target(
            targets.SqsQueue(
                self.openfda_queue,
                message=events.RuleTargetInput.from_object(
                    {"source": "scheduled", "trigger": "weekly_monday"}
                ),
            )
        )
