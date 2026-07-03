"""Prediction Stack — Leader detection, geographic affinity, timing estimation.

Deploys:
- DynamoDB tables (county configs, alert state, calibration history)
- Three Lambda functions for the prediction pipeline
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

        # --- Leader Detection Lambda ---
        self.leader_detection = _lambda.Function(
            self,
            "LeaderDetection",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/leader_detection"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=512,
            environment={
                "CALIBRATION_TABLE": self.calibration_table.table_name,
                "ALERT_STATE_TABLE": self.alert_state_table.table_name,
            },
        )
        self.calibration_table.grant_read_data(self.leader_detection)
        self.alert_state_table.grant_read_write_data(self.leader_detection)

        # --- Geographic Affinity Lambda ---
        self.geographic_affinity = _lambda.Function(
            self,
            "GeographicAffinity",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/geographic_affinity"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "COUNTY_CONFIG_TABLE": self.county_config_table.table_name,
            },
        )
        self.county_config_table.grant_read_data(self.geographic_affinity)

        # --- Timing Estimation Lambda ---
        self.timing_estimation = _lambda.Function(
            self,
            "TimingEstimation",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/prediction/timing_estimation"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "CALIBRATION_TABLE": self.calibration_table.table_name,
                "COUNTY_CONFIG_TABLE": self.county_config_table.table_name,
            },
        )
        self.calibration_table.grant_read_data(self.timing_estimation)
        self.county_config_table.grant_read_data(self.timing_estimation)
