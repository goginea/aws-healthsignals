"""Orchestration Stack — Pipeline coordinator Lambda + infrastructure.

Deploys:
- Pipeline coordinator Lambda (triggered by S3 data landing)
- S3 event notification on the data bucket
- DynamoDB pipeline_runs table (observability)
- Shared Lambda Layer
- IAM permissions for Lambda invocation + Step Functions StartExecution + EventBridge PutEvents

NOTE: Uses bucket name lookup (not cross-stack reference) to avoid
a CDK dependency cycle with the Ingestion stack.
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
        data_bucket_name: str = "",
        state_machine_arn: str = "",
        leader_detection_function_name: str = "healthsignals-leader-detection",
        geo_affinity_function_name: str = "healthsignals-geographic-affinity",
        timing_estimation_function_name: str = "healthsignals-timing-estimation",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolve data bucket by name (avoids cross-stack circular dependency)
        bucket_name = data_bucket_name or f"healthsignals-data-{self.account}-{self.region}"
        data_bucket = s3.Bucket.from_bucket_name(
            self, "DataBucketRef", bucket_name
        )

        # --- Shared Lambda Layer ---
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-orchestration",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities: config_loader, token_utils",
        )

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
            time_to_live_attribute="ttl",
        )

        # --- Pipeline Coordinator Lambda ---
        self.coordinator = _lambda.Function(
            self,
            "PipelineCoordinator",
            function_name="healthsignals-pipeline-coordinator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/orchestration/pipeline_coordinator"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(10),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "DATA_BUCKET": bucket_name,
                "CONFIG_BUCKET": bucket_name,
                "CONFIG_PREFIX": "config/",
                "LEADER_DETECTION_FUNCTION": leader_detection_function_name,
                "GEO_AFFINITY_FUNCTION": geo_affinity_function_name,
                "TIMING_ESTIMATION_FUNCTION": timing_estimation_function_name,
                "STATE_MACHINE_ARN": state_machine_arn,
                "PIPELINE_RUNS_TABLE": self.pipeline_runs_table.table_name,
                "ALERT_STATE_TABLE": "healthsignals-alert-state",
                "MAX_COUNTIES_PER_RUN": "20",
                "LOG_LEVEL": "INFO",
                "EVENT_BUS_NAME": "default",
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
            self.coordinator.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["states:StartExecution"],
                    resources=[
                        f"arn:aws:states:{self.region}:{self.account}:stateMachine:healthsignals-*"
                    ],
                )
            )

        # --- Permissions: EventBridge PutEvents (for downstream plugin modules) ---
        self.coordinator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[
                    f"arn:aws:events:{self.region}:{self.account}:event-bus/default",
                ],
            )
        )

        # --- S3 Event Notification: Trigger on new Delphi data ---
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.coordinator),
            s3.NotificationKeyFilter(prefix="raw/delphi/", suffix=".json"),
        )
