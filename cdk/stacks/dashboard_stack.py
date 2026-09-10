"""Dashboard Stack — admin-authenticated live status dashboard.

Deploys a reproducible, CDK-managed admin console for HealthSignals:
- Private S3 bucket for the static site (served only via CloudFront OAC)
- CloudFront distribution (origin access control to the bucket)
- Cognito User Pool + app client (admin login -> JWT)
- API Gateway REST API guarded by a Cognito User Pool authorizer
- Read-only Lambda backing the API (CloudFormation status/drift, Step Functions
  run history + per-run detail)

The API is NOT public: every route requires a valid Cognito ID token. An
initial admin user is created at deploy time (email via CDK context
'dashboard_admin_email').
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_cognito as cognito,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct


class DashboardStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        admin_email: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Static site bucket (private; CloudFront-only access) ---
        self.site_bucket = s3.Bucket(
            self,
            "DashboardSiteBucket",
            bucket_name=f"healthsignals-dashboard-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- CloudFront distribution (OAC to the private bucket) ---
        self.distribution = cloudfront.Distribution(
            self,
            "DashboardDistribution",
            comment="HealthSignals admin dashboard",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )
        cf_origin = f"https://{self.distribution.distribution_domain_name}"

        # --- Cognito User Pool (admin login) ---
        self.user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            user_pool_name="healthsignals-dashboard-admins",
            self_sign_up_enabled=False,  # admins are provisioned, not self-registered
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "DashboardWebClient",
            user_pool_client_name="healthsignals-dashboard-web",
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            id_token_validity=Duration.hours(8),
            access_token_validity=Duration.hours(8),
            prevent_user_existence_errors=True,
        )

        # --- Initial admin user (created at deploy when an email is provided) ---
        if admin_email:
            cognito.CfnUserPoolUser(
                self,
                "InitialAdminUser",
                user_pool_id=self.user_pool.user_pool_id,
                username=admin_email,
                desired_delivery_mediums=["EMAIL"],
                user_attributes=[
                    {"name": "email", "value": admin_email},
                    {"name": "email_verified", "value": "true"},
                ],
            )

        # --- Read-only API Lambda ---
        self.api_fn = _lambda.Function(
            self,
            "DashboardApiFunction",
            function_name="healthsignals-dashboard-api",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("../lambdas/dashboard/api"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "ALLOWED_ORIGIN": cf_origin,
                "PIPELINE_RUNS_TABLE": "healthsignals-pipeline-runs",
                "LOG_LEVEL": "INFO",
            },
        )

        # Scoped read-only permissions. CloudFormation/Step Functions describe
        # + list APIs don't support resource-level scoping for these actions, so
        # they are granted on "*" (all read-only, no mutation).
        self.api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:DetectStackDrift",
                    "cloudformation:DescribeStackDriftDetectionStatus",
                    "cloudformation:DescribeStackResourceDrifts",
                ],
                resources=["*"],
            )
        )
        self.api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "states:ListStateMachines",
                    "states:ListExecutions",
                    "states:DescribeExecution",
                ],
                resources=["*"],
            )
        )
        self.api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query", "dynamodb:Scan", "dynamodb:GetItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-pipeline-runs",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/healthsignals-pipeline-runs/index/*",
                ],
            )
        )

        # --- API Gateway with Cognito authorizer ---
        self.api = apigw.RestApi(
            self,
            "DashboardApi",
            rest_api_name="healthsignals-dashboard",
            description="HealthSignals admin dashboard read API (Cognito-protected)",
            deploy_options=apigw.StageOptions(stage_name="prod"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[cf_origin],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "DashboardAuthorizer",
            cognito_user_pools=[self.user_pool],
        )
        auth_method_opts = {
            "authorizer": authorizer,
            "authorization_type": apigw.AuthorizationType.COGNITO,
        }
        integ = apigw.LambdaIntegration(self.api_fn)

        # /status
        self.api.root.add_resource("status").add_method("GET", integ, **auth_method_opts)
        # /pipelines
        self.api.root.add_resource("pipelines").add_method("GET", integ, **auth_method_opts)
        # /runs
        self.api.root.add_resource("runs").add_method("GET", integ, **auth_method_opts)
        # /drift/{stack}
        drift = self.api.root.add_resource("drift")
        drift.add_resource("{stack}").add_method("POST", integ, **auth_method_opts)

        # --- Outputs (used to wire the frontend config at deploy time) ---
        CfnOutput(self, "DashboardUrl", value=cf_origin)
        CfnOutput(self, "DashboardApiUrl", value=self.api.url)
        CfnOutput(self, "DashboardUserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "DashboardUserPoolClientId",
                  value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "DashboardSiteBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "DashboardDistributionId",
                  value=self.distribution.distribution_id)
