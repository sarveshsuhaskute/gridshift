"""Worker region: where jobs actually run. Deployed once per worker region."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

CLUSTER_NAME = "gridshift-workers"
TASK_FAMILY = "gridshift-runner"
NETWORK_PARAMETER = "/gridshift/worker/network"
DEFAULT_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"
ECR_PUBLIC = "public.ecr.aws/"
CACHE_PREFIX = "ecr-public"
NETWORK_MODES = ("public", "private")


class WorkerRegionStack(Stack):
    @property
    def availability_zones(self) -> list[str]:
        # Pinned so synth needs no account lookups; "a" and "b" exist in every supported region.
        return [f"{self.region}a", f"{self.region}b"]

    def __init__(
        self, scope: Construct, construct_id: str, *, home_region: str, network_mode: str = "public", **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if network_mode not in NETWORK_MODES:
            raise ValueError(f"network_mode must be one of {NETWORK_MODES}")
        private = network_mode == "private"

        # public: subnets with no inbound rules and no NAT gateway. Tasks can pull images and call
        #   AWS APIs, and nothing can reach them. Saves ~$32/month per region versus NAT.
        # private: isolated subnets with no internet path at all. AWS APIs go through VPC endpoints
        #   and images through an ECR pull-through cache. Endpoints cost ~$0.01/hour each per AZ.
        subnet_type = ec2.SubnetType.PRIVATE_ISOLATED if private else ec2.SubnetType.PUBLIC
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name=network_mode, subnet_type=subnet_type)],
        )
        if private:
            vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
            for name, service in {
                "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
                "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
                "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            }.items():
                vpc.add_interface_endpoint(name, service=service, private_dns_enabled=True)
        security_group = ec2.SecurityGroup(
            self, "TaskSecurityGroup", vpc=vpc, description="GridShift job tasks: egress only", allow_all_outbound=True
        )

        cluster = ecs.Cluster(
            self, "Cluster", cluster_name=CLUSTER_NAME, vpc=vpc, enable_fargate_capacity_providers=True
        )

        bucket = s3.Bucket(
            self,
            "Outputs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        log_group = logs.LogGroup(
            self,
            "JobLogs",
            log_group_name="/gridshift/jobs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Size is a placeholder: every RunTask overrides cpu/memory with the job's size.
        task_definition = ecs.FargateTaskDefinition(self, "Runner", family=TASK_FAMILY, cpu=256, memory_limit_mib=512)
        image = self.node.try_get_context("job_image") or DEFAULT_IMAGE
        if private:
            image = self._cache_through_ecr(image, task_definition)
        task_definition.add_container(
            "job",
            container_name="job",
            image=ecs.ContainerImage.from_registry(image),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="job", log_group=log_group),
            environment={"GRIDSHIFT_REGION": self.region},
        )
        bucket.grant_put(task_definition.task_role, "jobs/*")

        ssm.StringParameter(
            self,
            "WorkerConfig",
            parameter_name=NETWORK_PARAMETER,
            description="GridShift worker discovery, read by the control plane dispatcher",
            string_value=self.to_json_string(
                {
                    "cluster": cluster.cluster_name,
                    "task_definition_family": TASK_FAMILY,
                    "subnets": vpc.select_subnets(subnet_type=subnet_type).subnet_ids,
                    "assign_public_ip": "DISABLED" if private else "ENABLED",
                    "security_groups": [security_group.security_group_id],
                    "bucket": bucket.bucket_name,
                }
            ),
        )

        # Forward "task stopped" events to the control plane. The home region's own events
        # are already on its default bus, and a bus can't target itself.
        if self.region != home_region:
            events.Rule(
                self,
                "ForwardTaskStopped",
                description="Send GridShift task completions to the control plane region",
                event_pattern=events.EventPattern(
                    source=["aws.ecs"],
                    detail_type=["ECS Task State Change"],
                    detail={
                        "clusterArn": [cluster.cluster_arn],
                        "lastStatus": ["STOPPED"],
                        "startedBy": events.Match.prefix("gridshift-"),
                    },
                ),
                targets=[
                    targets.EventBus(
                        events.EventBus.from_event_bus_arn(
                            self,
                            "HomeBus",
                            f"arn:{self.partition}:events:{home_region}:{self.account}:event-bus/default",
                        )
                    )
                ],
            )

    def _cache_through_ecr(self, image: str, task_definition: ecs.FargateTaskDefinition) -> str:
        """Serve an ECR Public image from this region's private ECR via a pull-through cache."""
        if not image.startswith(ECR_PUBLIC):
            raise ValueError(f"private network mode needs a public.ecr.aws image, got {image!r}")
        ecr.CfnPullThroughCacheRule(
            self, "EcrPublicCache", ecr_repository_prefix=CACHE_PREFIX, upstream_registry_url=ECR_PUBLIC.rstrip("/")
        )
        execution_role = task_definition.obtain_execution_role()
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    # The first pull creates the cache repository and imports the image.
                    "ecr:CreateRepository",
                    "ecr:BatchImportUpstreamImage",
                ],
                resources=[self.format_arn(service="ecr", resource="repository", resource_name=f"{CACHE_PREFIX}/*")],
            )
        )
        return f"{self.account}.dkr.ecr.{self.region}.{self.url_suffix}/{CACHE_PREFIX}/{image.removeprefix(ECR_PUBLIC)}"
