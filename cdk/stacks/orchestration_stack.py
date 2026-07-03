"""Orchestration Stack — Pipeline coordinator Lambda + infrastructure.

Deploys:
- Pipeline coordinator Lambda (triggered by S3 data landing)
- S3 event notification on the data bucket
- DynamoDB pipeline_runs table (observability)
- IAM permissions for Lambda invocation + Step Functions StartExecution
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
)
from constructs import Construct


class OrchestrationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_bucket: s3.IBucket,
        state_machine_arn: str = "",
        leader_detection_function_name: str = "healthsignals-leader-detection",
        geo_affinity_function_name: str = "healthsignals-geographic-affinity",
        timing_estimation_function_name: str = "healthsignals-timing-estimation",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- DynamoDB: Pipeline Runs (observability) ---
        self.pipeline_runs_table = dynamodb.Table(
            self,
            "PipelineRunsTable",
            table_name="healthsignals-pipeline-runs",
            partition_key=dynamodb.Attribute(
                name="execution_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",  # Auto-expire old records after 90 days
        )

        # --- Pipeline Coordinator Lambda ---
        self.coordinator = _lambda.Function(
            self,
            "PipelineCoordinator",
            function_name="healthsignals-pipeline-coordinator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/orchestration/pipeline_coordinator"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(10),  # Needs time for synchronous Lambda calls
            memory_size=512,
            environment={
                "DATA_BUCKET": data_bucket.bucket_name,
                "CONFIG_BUCKET": data_bucket.bucket_name,
                "CONFIG_PREFIX": "config/",
                "LEADER_DETECTION_FUNCTION": leader_detection_function_name,
                "GEO_AFFINITY_FUNCTION": geo_affinity_function_name,
                "TIMING_ESTIMATION_FUNCTION": timing_estimation_function_name,
                "STATE_MACHINE_ARN": state_machine_arn,
                "PIPELINE_RUNS_TABLE": self.pipeline_runs_table.table_name,
                "ALERT_STATE_TABLE": "healthsignals-alert-state",
                "MAX_COUNTIES_PER_RUN": "20",
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Pipeline coordinator: chains ingestion → prediction → generation. "
                "Triggered when new Delphi data lands in S3."
            ),
        )

        # --- Permissions: S3 read ---
        data_bucket.grant_read(self.coordinator)

        # --- Permissions: DynamoDB read/write ---
        self.pipeline_runs_table.grant_read_write_data(self.coordinator)

        # Grant access to alert_state table (in prediction stack)
        self.coordinator.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:Query",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-alert-state",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-county-configs",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-calibration",
                ],
            )
        )

        # --- Permissions: Invoke prediction Lambdas ---
        self.coordinator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{leader_detection_function_name}",
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{geo_affinity_function_name}",
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{timing_estimation_function_name}",
                ],
            )
        )

        # --- Permissions: Start Step Functions executions ---
        if state_machine_arn:
            self.coordinator.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["states:StartExecution"],
                    resources=[state_machine_arn],
                )
            )
        else:
            # Wildcard for development — scope down for production
            self.coordinator.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["states:StartExecution"],
                    resources=[
                        f"arn:aws:states:{self.region}:{self.account}:stateMachine:healthsignals-*"
                    ],
                )
            )

        # --- S3 Event Notification: Trigger on new Delphi data ---
        # Trigger when any file is created under raw/delphi/
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.coordinator),
            s3.NotificationKeyFilter(prefix="raw/delphi/", suffix=".json"),
        )
