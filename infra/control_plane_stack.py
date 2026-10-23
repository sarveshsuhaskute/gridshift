"""Control plane (home region): API, planner, workflow, completion handling, observability."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from infra.worker_stack import CLUSTER_NAME, NETWORK_PARAMETER, TASK_FAMILY

API_KEY_PARAMETER = "/gridshift/electricitymaps/api-key"


class ControlPlaneStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        worker_regions: list[str],
        carbon_provider: str,
        alert_email: str | None,
        monthly_budget_usd: float,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        jobs = dynamodb.Table(
            self,
            "Jobs",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        forecasts = dynamodb.Table(
            self,
            "Forecasts",
            partition_key=dynamodb.Attribute(name="region", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        topic = sns.Topic(self, "Notifications", display_name="GridShift", enforce_ssl=True)
        if alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))

        environment = {
            "HOME_REGION": self.region,
            "WORKER_REGIONS": ",".join(worker_regions),
            "JOBS_TABLE": jobs.table_name,
            "FORECASTS_TABLE": forecasts.table_name,
            "TOPIC_ARN": topic.topic_arn,
            "CARBON_PROVIDER": carbon_provider,
            "API_KEY_PARAMETER": API_KEY_PARAMETER,
        }
        code = lambda_.Code.from_asset("src", exclude=["**/__pycache__"])

        def function(name: str, timeout: int = 30) -> lambda_.Function:
            construct_id = name.title().replace("_", "")
            return lambda_.Function(
                self,
                construct_id,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=f"handlers.{name}.handler",
                code=code,
                timeout=Duration.seconds(timeout),
                memory_size=256,
                environment=environment,
                tracing=lambda_.Tracing.ACTIVE,
                log_group=logs.LogGroup(
                    self,
                    f"{construct_id}Logs",
                    retention=logs.RetentionDays.TWO_WEEKS,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            )

        submit_fn = function("submit_job")
        get_fn = function("get_job")
        plan_fn = function("plan_job")
        dispatch_fn = function("dispatch_job")
        complete_fn = function("complete_job")
        finalize_fn = function("finalize_job")
        ingest_fn = function("ingest_forecasts", timeout=120)

        jobs.grant_read_write_data(submit_fn)
        jobs.grant_read_data(get_fn)
        jobs.grant_read_write_data(plan_fn)
        forecasts.grant_read_data(plan_fn)
        jobs.grant_read_write_data(dispatch_fn)
        jobs.grant_read_write_data(complete_fn)
        jobs.grant_read_write_data(finalize_fn)
        topic.grant_publish(finalize_fn)
        forecasts.grant_write_data(ingest_fn)
        ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(service="ssm", resource="parameter", resource_name=API_KEY_PARAMETER[1:])],
            )
        )

        # The dispatcher may only start the GridShift task family on the GridShift cluster.
        dispatch_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[
                    f"arn:{self.partition}:ecs:{region}:{self.account}:task-definition/{TASK_FAMILY}:*"
                    for region in worker_regions
                ],
                conditions={
                    "ArnEquals": {
                        "ecs:cluster": [
                            f"arn:{self.partition}:ecs:{region}:{self.account}:cluster/{CLUSTER_NAME}"
                            for region in worker_regions
                        ]
                    }
                },
            )
        )
        dispatch_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:TagResource"],
                resources=[f"arn:{self.partition}:ecs:{region}:{self.account}:task/*" for region in worker_regions],
                conditions={"StringEquals": {"ecs:CreateAction": "RunTask"}},
            )
        )
        dispatch_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=["*"],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            )
        )
        dispatch_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:{self.partition}:ssm:{region}:{self.account}:parameter{NETWORK_PARAMETER}"
                    for region in worker_regions
                ],
            )
        )

        # Workflow -------------------------------------------------------------------------
        finalize_failed = tasks.LambdaInvoke(
            self,
            "FinalizeFailed",
            lambda_function=finalize_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                    "outcome": "FAILED",
                    "error": sfn.JsonPath.object_at("$.error"),
                }
            ),
            result_path=sfn.JsonPath.DISCARD,
        ).next(sfn.Fail(self, "JobFailed", error="JobFailed", cause="See the job record for details"))

        plan_task = tasks.LambdaInvoke(
            self,
            "Plan",
            lambda_function=plan_fn,
            payload=sfn.TaskInput.from_object({"job_id": sfn.JsonPath.string_at("$.job_id")}),
            payload_response_only=True,
            result_path="$.plan",
        )
        plan_task.add_catch(finalize_failed, result_path="$.error")

        wait = sfn.Wait(self, "WaitForCleanSlot", time=sfn.WaitTime.timestamp_path("$.plan.start_at"))

        def dispatch(name: str, region_path: str, capacity: str) -> tasks.LambdaInvoke:
            return tasks.LambdaInvoke(
                self,
                name,
                lambda_function=dispatch_fn,
                integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
                payload=sfn.TaskInput.from_object(
                    {
                        "job_id": sfn.JsonPath.string_at("$.job_id"),
                        "region": sfn.JsonPath.string_at(region_path),
                        "capacity_provider": capacity,
                        "task_token": sfn.JsonPath.task_token,
                    }
                ),
                task_timeout=sfn.Timeout.at("$.plan.timeout_seconds"),
                result_path="$.run",
            )

        run_spot = dispatch("RunOnSpot", "$.plan.region", "FARGATE_SPOT")
        run_fallback = dispatch("RunFallbackOnDemand", "$.plan.fallback_region", "FARGATE")
        run_fallback.add_catch(finalize_failed, result_path="$.error")
        run_spot.add_catch(run_fallback, result_path="$.spot_error")

        finalize_ok = tasks.LambdaInvoke(
            self,
            "FinalizeSucceeded",
            lambda_function=finalize_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                    "outcome": "SUCCEEDED",
                    "run": sfn.JsonPath.object_at("$.run"),
                }
            ),
            result_path=sfn.JsonPath.DISCARD,
        )
        run_fallback.next(finalize_ok)

        definition = plan_task.next(wait).next(run_spot).next(finalize_ok)
        state_machine = sfn.StateMachine(
            self,
            "JobWorkflow",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            tracing_enabled=True,
            timeout=Duration.days(8),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self, "WorkflowLogs", retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY
                ),
                level=sfn.LogLevel.ERROR,
            ),
        )
        state_machine.grant_start_execution(submit_fn)
        state_machine.grant_task_response(complete_fn)
        submit_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)

        # Task completions from every worker region land on this region's default bus. Events that
        # still fail after retries are kept in a DLQ and raise an alarm; the job itself recovers
        # through its task timeout and the on-demand fallback.
        completion_dlq = sqs.Queue(
            self,
            "CompletionDlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        completion_dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(5)).create_alarm(
            self,
            "CompletionDlqAlarm",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A GridShift task completion event could not be processed",
        ).add_alarm_action(cw_actions.SnsAction(topic))
        events.Rule(
            self,
            "TaskStopped",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={"lastStatus": ["STOPPED"], "startedBy": events.Match.prefix("gridshift-")},
            ),
            targets=[targets.LambdaFunction(complete_fn, retry_attempts=4, dead_letter_queue=completion_dlq)],
        )
        events.Rule(
            self,
            "IngestSchedule",
            schedule=events.Schedule.rate(Duration.minutes(30)),
            targets=[targets.LambdaFunction(ingest_fn)],
        )

        # API ------------------------------------------------------------------------------
        api = apigw.HttpApi(self, "Api", default_authorizer=authorizers.HttpIamAuthorizer())
        # Caps request rate so a runaway client can't flood the account with jobs.
        api.default_stage.node.default_child.add_property_override(
            "DefaultRouteSettings", {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}
        )
        api.add_routes(
            path="/jobs",
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("Submit", submit_fn),
        )
        api.add_routes(
            path="/jobs/{job_id}",
            methods=[apigw.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("Get", get_fn),
        )

        # Observability --------------------------------------------------------------------
        failed_alarm = state_machine.metric_failed(period=Duration.minutes(5)).create_alarm(
            self,
            "JobFailuresAlarm",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A GridShift job failed on both Spot and the on-demand fallback",
        )
        failed_alarm.add_alarm_action(cw_actions.SnsAction(topic))

        def gridshift_metric(name: str, label: str) -> cw.Metric:
            return cw.Metric(
                namespace="GridShift", metric_name=name, statistic="Sum", period=Duration.days(1), label=label
            )

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="GridShift")
        dashboard.add_widgets(
            cw.SingleValueWidget(
                title="CO2e saved (g, 7 days)",
                metrics=[
                    cw.MathExpression(
                        expression="SUM(SEARCH('{GridShift,Region} MetricName=\"CarbonSavedGrams\"', 'Sum', 86400))",
                        label="saved",
                        period=Duration.days(1),
                    )
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Jobs per day",
                left=[
                    state_machine.metric_succeeded(period=Duration.days(1)),
                    state_machine.metric_failed(period=Duration.days(1)),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Carbon-intensity forecasts ingested",
                left=[gridshift_metric("ForecastPointsStored", "points")],
                right=[gridshift_metric("ForecastRegionsFailed", "regions failed")],
                width=8,
            ),
        )

        if alert_email:
            budgets.CfnBudget(
                self,
                "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=monthly_budget_usd, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type=kind,
                            threshold=80,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(address=alert_email, subscription_type="EMAIL")
                        ],
                    )
                    for kind in ("ACTUAL", "FORECASTED")
                ],
            )

        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "JobsTable", value=jobs.table_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
