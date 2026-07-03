"""Generation Stack — Step Functions workflow + Bedrock integration.

Deploys:
- Step Functions state machine (4-step alert generation)
- Bedrock model access IAM policies
- Bedrock Guardrails configuration
- Knowledge Base references
"""
from aws_cdk import (
    Stack,
    Duration,
    aws_stepfunctions as sfn,
    aws_iam as iam,
)
from constructs import Construct
import json


class GenerationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Bedrock IAM Role ---
        self.bedrock_role = iam.Role(
            self,
            "BedrockInvocationRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Allows Step Functions to invoke Bedrock models",
        )

        # Grant InvokeModel for Haiku and Sonnet
        self.bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    f"arn:aws:bedrock:{self.region}::foundation-model/us.anthropic.claude-sonnet-5",
                ],
            )
        )

        # Grant Knowledge Base retrieval (scoped to account's KBs)
        self.bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*",
                ],
            )
        )

        # Grant Guardrails (scoped to account's guardrails)
        self.bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*",
                ],
            )
        )

        # --- Step Functions State Machine ---
        self.state_machine = sfn.StateMachine(
            self,
            "AlertGenerationWorkflow",
            state_machine_name="healthsignals-alert-generation",
            definition_body=sfn.DefinitionBody.from_file(
                "../stepfunctions/alert_generation.asl.json"
            ),
            role=self.bedrock_role,
            timeout=Duration.minutes(10),
            tracing_enabled=True,  # X-Ray integration
        )
