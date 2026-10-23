"""Worker region: where jobs actually run. Deployed once per worker region."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

CLUSTER_NAME = "gridshift-workers"
TASK_FAMILY = "gridshift-runner"
NETWORK_PARAMETER = "/gridshift/worker/network"
DEFAULT_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"


class WorkerRegionStack(Stack):
    @property
    def availability_zones(self) -> list[str]:
        # Pinned so synth needs no account lookups; "a" and "b" exist in every supported region.
        return [f"{self.region}a", f"{self.region}b"]

    def __init__(self, scope: Construct, construct_id: str, *, home_region: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Public subnets with no inbound rules and no NAT gateway: tasks can pull images and
        # call AWS APIs, and nothing can reach them. Saves ~$32/month per region versus NAT.
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)],
        )
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
        task_definition.add_container(
            "job",
            container_name="job",
            image=ecs.ContainerImage.from_registry(self.node.try_get_context("job_image") or DEFAULT_IMAGE),
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
                    "subnets": vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC).subnet_ids,
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
