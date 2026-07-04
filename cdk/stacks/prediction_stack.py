"""Prediction Stack — Leader detection, geographic affinity, timing estimation.

Deploys:
- DynamoDB tables (county configs, alert state, calibration history)
- Three Lambda functions for the prediction pipeline
- Shared Lambda Layer for config_loader
- IAM roles with least-privilege access
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_iam as iam,
)
from constructs import Construct


class PredictionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Shared Lambda Layer ---
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-prediction",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities: config_loader, token_utils",
        )

        # --- DynamoDB: County Configuration Table ---
        self.county_config_table = dynamodb.Table(
            self,
            "CountyConfigTable",
            table_name="healthsignals-county-configs",
            partition_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- DynamoDB: Alert State Machine ---
        self.alert_state_table = dynamodb.Table(
            self,
            "AlertStateTable",
            table_name="healthsignals-alert-state",
            partition_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="disease_season", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # --- DynamoDB: Historical Calibration Table ---
        self.calibration_table = dynamodb.Table(
            self,
            "CalibrationTable",
            table_name="healthsignals-calibration",
            partition_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="disease_season", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
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

        # --- Leader Detection Lambda ---
        self.leader_detection = _lambda.Function(
            self,
            "LeaderDetection",
            function_name="healthsignals-leader-detection",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/leader_detection"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=512,
            layers=[self.shared_layer],
            environment={
                "CALIBRATION_TABLE": self.calibration_table.table_name,
                "ALERT_STATE_TABLE": self.alert_state_table.table_name,
                "CONFIG_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
                "CONFIG_PREFIX": "config/",
            },
        )
        self.calibration_table.grant_read_data(self.leader_detection)
        self.alert_state_table.grant_read_write_data(self.leader_detection)

        # --- Geographic Affinity Lambda ---
        self.geographic_affinity = _lambda.Function(
            self,
            "GeographicAffinity",
            function_name="healthsignals-geographic-affinity",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/geographic_affinity"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "COUNTY_CONFIG_TABLE": self.county_config_table.table_name,
                "CONFIG_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
                "CONFIG_PREFIX": "config/",
            },
        )
        self.county_config_table.grant_read_data(self.geographic_affinity)

        # --- Timing Estimation Lambda ---
        self.timing_estimation = _lambda.Function(
            self,
            "TimingEstimation",
            function_name="healthsignals-timing-estimation",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/timing_estimation"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[self.shared_layer],
            environment={
                "CALIBRATION_TABLE": self.calibration_table.table_name,
                "COUNTY_CONFIG_TABLE": self.county_config_table.table_name,
                "CONFIG_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
                "CONFIG_PREFIX": "config/",
            },
        )
        self.calibration_table.grant_read_data(self.timing_estimation)
        self.county_config_table.grant_read_data(self.timing_estimation)

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
                "DATA_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
                "CONFIG_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
                "CONFIG_PREFIX": "config/",
                "SHORTAGE_STATE_TABLE": self.shortage_state_table.table_name,
                "SHORTAGE_ALERTS_TABLE": self.shortage_alerts_table.table_name,
                "STATE_MACHINE_ARN": "",  # Will be set via environment override
            },
        )
        self.shortage_state_table.grant_read_write_data(self.shortage_change_detector)
        self.shortage_alerts_table.grant_read_write_data(self.shortage_change_detector)

        # S3 read permission for shortage change detector (raw shortage data and config)
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/raw/openfda-shortages/*",
                    f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/config/*",
                ],
            )
        )

        # Step Functions invoke permission for shortage change detector
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[
                    f"arn:aws:states:{self.region}:{self.account}:stateMachine:healthsignals-*"
                ],
            )
        )

        # CloudWatch PutMetricData permission for shortage change detector
        self.shortage_change_detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # Grant S3 read for config access
        self.leader_detection.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/config/*"],
            )
        )
        self.geographic_affinity.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/config/*"],
            )
        )
        self.timing_estimation.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/config/*"],
            )
        )
