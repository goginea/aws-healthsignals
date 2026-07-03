"""Delivery Stack — SES email + SNS SMS alert routing.

Deploys:
- SNS topic for SMS alerts
- SES configuration for email briefs
- DynamoDB feedback table
- Feedback recalibrator Lambda
- Alert dispatcher Lambda
- Feedback collector Lambda + API Gateway endpoint
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_ses as ses,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class DeliveryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- DynamoDB: Feedback Table ---
        self.feedback_table = dynamodb.Table(
            self,
            "FeedbackTable",
            table_name="healthsignals-feedback",
            partition_key=dynamodb.Attribute(
                name="alert_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="submitted_at", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )
        # GSI for querying feedback by county (needed by recalibrator)
        self.feedback_table.add_global_secondary_index(
            index_name="county-index",
            partition_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
        )

        # --- SNS Topic for SMS Alerts ---
        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name="healthsignals-alerts",
            display_name="HealthSignals Alert",
        )

        # --- Alert Dispatcher Lambda ---
        self.alert_dispatcher = _lambda.Function(
            self,
            "AlertDispatcher",
            function_name="healthsignals-alert-dispatcher",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/delivery/alert_dispatcher"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "SNS_TOPIC_ARN": self.alert_topic.topic_arn,
                "SENDER_EMAIL": self.node.try_get_context("alert_sender_email")
                or "alerts@healthsignals.example.com",
                "SUBSCRIPTIONS_TABLE": "healthsignals-subscriptions",
            },
        )

        # Grant SES send (scoped to account's verified identities)
        self.alert_dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/*"],
            )
        )
        self.alert_topic.grant_publish(self.alert_dispatcher)

        # Grant DynamoDB read for subscriptions table
        self.alert_dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-subscriptions",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-subscriptions/index/*",
                ],
            )
        )

        # --- Feedback Collector Lambda + API ---
        self.feedback_collector = _lambda.Function(
            self,
            "FeedbackCollector",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/delivery/feedback_collector"),
            handler="handler.lambda_handler",
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "FEEDBACK_TABLE": "healthsignals-feedback",
                "RECALIBRATOR_FUNCTION_NAME": "healthsignals-feedback-recalibrator",
                "RECALIBRATION_THRESHOLD": "3",
            },
        )

        # Grant DynamoDB write + query for feedback (need query to count responses)
        self.feedback_collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:Query"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-feedback",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-feedback/index/*",
                ],
            )
        )

        # Grant Lambda invoke for triggering recalibrator
        self.feedback_collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:healthsignals-feedback-recalibrator",
                ],
            )
        )

        # --- Feedback Recalibrator Lambda ---
        self.feedback_recalibrator = _lambda.Function(
            self,
            "FeedbackRecalibrator",
            function_name="healthsignals-feedback-recalibrator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            code=_lambda.Code.from_asset("../lambdas/delivery/feedback_recalibrator"),
            handler="handler.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "FEEDBACK_TABLE": "healthsignals-feedback",
                "CALIBRATION_TABLE": "healthsignals-calibration",
            },
        )

        # Grant DynamoDB read/write for recalibration
        self.feedback_recalibrator.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Scan", "dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-feedback",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-calibration",
                ],
            )
        )

        # --- API Gateway for Feedback ---
        self.feedback_api = apigw.RestApi(
            self,
            "FeedbackApi",
            rest_api_name="healthsignals-feedback",
            description="HealthSignals Feedback Collection API",
        )

        feedback_resource = self.feedback_api.root.add_resource("feedback")
        feedback_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.feedback_collector),
        )
