"""Subscription Stack — API Gateway + Lambda + DynamoDB for county subscription management.

Deploys:
- Secrets Manager secret for token signing
- DynamoDB subscriptions table (PK: county_fips, SK: subscription_id)
- API Gateway REST API with 5 endpoints
- Lambda functions for each subscription operation
- IAM roles with least-privilege access
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class SubscriptionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Secrets Manager: Token Signing Secret ---
        self.token_secret = secretsmanager.Secret(
            self,
            "TokenSigningSecret",
            secret_name="healthsignals/token-signing-key",
            description="HMAC signing key for subscription verification and unsubscribe tokens",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=64,
            ),
        )

        # --- DynamoDB Subscriptions Table ---
        self.subscriptions_table = dynamodb.Table(
            self,
            "SubscriptionsTable",
            table_name="healthsignals-subscriptions",
            partition_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="subscription_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # GSI: Look up by status (for batch operations like expiry checks)
        self.subscriptions_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="updated_at", type=dynamodb.AttributeType.STRING
            ),
        )

        # GSI: Look up by state (for state-level admin queries)
        self.subscriptions_table.add_global_secondary_index(
            index_name="state-index",
            partition_key=dynamodb.Attribute(
                name="state", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
        )

        # --- Shared Lambda Environment ---
        lambda_env = {
            "SUBSCRIPTIONS_TABLE": self.subscriptions_table.table_name,
            "CONFIG_BUCKET": "",  # Set at deploy time
            "CONFIG_PREFIX": "config/",
            "LOG_LEVEL": "INFO",
            "API_BASE_URL": "",  # Set after API Gateway creation
            "TOKEN_SECRET_ARN": self.token_secret.secret_arn,
            "ENVIRONMENT": "production",
        }

        # --- Lambda Functions ---
        self.subscribe_fn = self._create_lambda(
            "SubscribeFunction",
            handler_path="../lambdas/subscription/subscribe",
            env=lambda_env,
        )

        self.verify_fn = self._create_lambda(
            "VerifyFunction",
            handler_path="../lambdas/subscription/verify",
            env=lambda_env,
        )

        self.unsubscribe_fn = self._create_lambda(
            "UnsubscribeFunction",
            handler_path="../lambdas/subscription/unsubscribe",
            env=lambda_env,
        )

        self.update_fn = self._create_lambda(
            "UpdatePreferencesFunction",
            handler_path="../lambdas/subscription/update_preferences",
            env=lambda_env,
        )

        self.status_fn = self._create_lambda(
            "StatusFunction",
            handler_path="../lambdas/subscription/status",
            env=lambda_env,
        )

        # --- Grant DynamoDB + SES + Secrets Manager Permissions ---
        for fn in [self.subscribe_fn, self.verify_fn, self.unsubscribe_fn, self.update_fn, self.status_fn]:
            self.subscriptions_table.grant_read_write_data(fn)
            self.token_secret.grant_read(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ses:SendEmail"],
                    resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/*"],
                )
            )

        # --- API Gateway ---
        self.api = apigw.RestApi(
            self,
            "SubscriptionApi",
            rest_api_name="healthsignals-subscription",
            description="HealthSignals County Subscription Management API",
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )

        subscription_resource = self.api.root.add_resource("subscription")

        # POST /subscription/subscribe
        subscribe_resource = subscription_resource.add_resource("subscribe")
        subscribe_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.subscribe_fn),
        )

        # GET /subscription/verify
        verify_resource = subscription_resource.add_resource("verify")
        verify_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.verify_fn),
        )

        # GET+POST /subscription/unsubscribe
        unsubscribe_resource = subscription_resource.add_resource("unsubscribe")
        unsubscribe_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.unsubscribe_fn),
        )
        unsubscribe_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.unsubscribe_fn),
        )

        # PUT /subscription/preferences
        preferences_resource = subscription_resource.add_resource("preferences")
        preferences_resource.add_method(
            "PUT",
            apigw.LambdaIntegration(self.update_fn),
        )

        # GET /subscription/status
        status_resource = subscription_resource.add_resource("status")
        status_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.status_fn),
        )

    def _create_lambda(self, id: str, handler_path: str, env: dict) -> _lambda.Function:
        """Create a Lambda function with standard configuration."""
        return _lambda.Function(
            self,
            id,
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(handler_path),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=env,
        )
