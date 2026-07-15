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
    def __init__(self, scope: Construct, construct_id: str, plugin_gsis: list[dict] = None, **kwargs) -> None:
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

        # --- Shared Lambda Layer ---
        self.shared_layer = _lambda.LayerVersion(
            self,
            "SharedUtilsLayer",
            layer_version_name="healthsignals-shared-subscription",
            code=_lambda.Code.from_asset("../layers/shared"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Shared utilities: config_loader, token_utils",
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

        self.subscriptions_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="updated_at", type=dynamodb.AttributeType.STRING
            ),
        )

        self.subscriptions_table.add_global_secondary_index(
            index_name="state-index",
            partition_key=dynamodb.Attribute(
                name="state", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="county_fips", type=dynamodb.AttributeType.STRING
            ),
        )

        # Plugin GSIs — dynamically added by plugin modules
        for gsi in (plugin_gsis or []):
            gsi_kwargs = {
                "index_name": gsi["index_name"],
                "partition_key": dynamodb.Attribute(
                    name=gsi["partition_key"], type=dynamodb.AttributeType.STRING
                ),
            }
            if gsi.get("sort_key"):
                gsi_kwargs["sort_key"] = dynamodb.Attribute(
                    name=gsi["sort_key"], type=dynamodb.AttributeType.STRING
                )
            self.subscriptions_table.add_global_secondary_index(**gsi_kwargs)

        # --- Shared Lambda Environment ---
        lambda_env = {
            "SUBSCRIPTIONS_TABLE": self.subscriptions_table.table_name,
            "CONFIG_BUCKET": f"healthsignals-data-{self.account}-{self.region}",
            "CONFIG_PREFIX": "config/",
            "LOG_LEVEL": "INFO",
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
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[f"arn:aws:s3:::healthsignals-data-{self.account}-{self.region}/config/*"],
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

        subscribe_resource = subscription_resource.add_resource("subscribe")
        subscribe_resource.add_method("POST", apigw.LambdaIntegration(self.subscribe_fn))

        verify_resource = subscription_resource.add_resource("verify")
        verify_resource.add_method("GET", apigw.LambdaIntegration(self.verify_fn))

        unsubscribe_resource = subscription_resource.add_resource("unsubscribe")
        unsubscribe_resource.add_method("GET", apigw.LambdaIntegration(self.unsubscribe_fn))
        unsubscribe_resource.add_method("POST", apigw.LambdaIntegration(self.unsubscribe_fn))

        preferences_resource = subscription_resource.add_resource("preferences")
        preferences_resource.add_method("PUT", apigw.LambdaIntegration(self.update_fn))

        status_resource = subscription_resource.add_resource("status")
        status_resource.add_method("GET", apigw.LambdaIntegration(self.status_fn))

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
            layers=[self.shared_layer],
            environment=env,
        )
