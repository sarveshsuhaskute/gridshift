"""Keyless CI/CD: lets GitHub Actions deploy GridShift through OIDC, with no stored AWS keys."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_URL = "token.actions.githubusercontent.com"
DEPLOY_ENVIRONMENT = "production"


class GitHubDeployStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_repo: str,
        existing_provider_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # An account can only have one provider per URL, so reuse it if one already exists.
        if existing_provider_arn:
            provider_arn = existing_provider_arn
        else:
            provider_arn = iam.CfnOIDCProvider(
                self, "GitHubOidc", url=f"https://{GITHUB_OIDC_URL}", client_id_list=["sts.amazonaws.com"]
            ).attr_arn

        # Only workflow jobs running in the repo's "production" environment can assume the role,
        # so GitHub environment protection rules (required reviewers, branch limits) gate deploys.
        role = iam.Role(
            self,
            "DeployRole",
            description=f"GitHub Actions deploys for {github_repo}",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC_URL}:sub": f"repo:{github_repo}:environment:{DEPLOY_ENVIRONMENT}",
                    }
                },
            ),
        )
        # CDK deploys through the roles created by `cdk bootstrap`; the pipeline needs nothing else.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:{self.partition}:iam::{self.account}:role/cdk-hnb659fds-*"],
            )
        )

        CfnOutput(self, "DeployRoleArn", value=role.role_arn)
