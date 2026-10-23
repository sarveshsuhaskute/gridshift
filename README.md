# GridShift

**Carbon-aware, multi-region batch scheduling on AWS.**

GridShift runs your batch jobs in the AWS region and time slot where electricity is cleanest.
It keeps every job within its deadline, cost budget and data-residency rules, and accounts for
the cost of moving data between regions.

```text
$ make plan
region        zone            now gCO2e/kWh
us-east-1     US-MIDA-PJM      380
ca-central-1  CA-QC             35
eu-north-1    SE-SE3            25
...
explanation: Run in eu-north-1 now at ~25 gCO2e/kWh: 93% less CO2e than running now in us-east-1, cost +10.0%.
```

## Why

- The grid powering `us-east-1` emits about 15x more CO₂ per kWh than the grid powering `eu-north-1`.
- Most batch work (reports, encoding, ETL, simulations) is flexible about *where* and *when* it runs.
- AWS reports your carbon footprint after the fact. Nothing on the platform *schedules* around it.

## What it does

| Capability | How |
|---|---|
| Multi-region placement | Scores every (region × 30-min slot) candidate before the deadline |
| Time shifting | Step Functions `Wait` holds the job until a cleaner slot, at no cost |
| Data gravity | Inter-region transfer cost and energy count against remote regions |
| Data residency | `allowed_regions` is a hard constraint |
| Cost guardrail | `max_cost_overhead_pct` vs. running now at home |
| Spot-first, self-healing | Fargate Spot. If interrupted, it falls back once to on-demand in another region. |
| Measured results | Actual runtime → CO₂ emitted and saved, as CloudWatch metrics and SNS notifications |
| Degrades gracefully | If the carbon API is down, it uses static regional averages and labels the decision |

## Architecture

![GridShift architecture](docs/architecture.svg)

The control plane runs in one home region: API Gateway (IAM auth), Lambda, Step Functions,
DynamoDB, EventBridge, SNS and CloudWatch. Worker stacks run in each worker region: a VPC,
an ECS Fargate cluster, an S3 output bucket and an SSM discovery parameter. Task completions
travel back to the control plane over **cross-region EventBridge**, so nothing polls.

Diagrams, the carbon model, trade-offs and the security design are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

**AWS services used:** Lambda · Step Functions · ECS Fargate (Spot) · EventBridge (cross-region) ·
DynamoDB (TTL, PITR) · API Gateway HTTP API (IAM auth) · S3 · SSM Parameter Store · SNS ·
CloudWatch (EMF metrics, dashboard, alarms) · X-Ray · VPC · IAM · AWS Budgets · CDK / CloudFormation

## Repository layout

```
app.py                     CDK app: 1 control plane + N worker stacks
infra/                     CDK stacks (control plane, worker region)
src/gridshift/             Decision engine (pure Python, no AWS dependencies)
  scheduler.py             Candidate scoring and selection
  energy.py                Energy model (Cloud Carbon Footprint coefficients)
  carbon.py                Carbon-intensity providers (Electricity Maps, static)
  regions.py               Region profiles: grid zone, prices, fallback intensity
  prices.json              Fargate + data-transfer prices from the AWS Price List
src/handlers/              Lambda handlers (submit, plan, dispatch, complete, finalize, ingest, get)
scripts/plan_locally.py    Run the planner locally, no AWS account needed
scripts/gridshift_cli.py   SigV4-signed client for the deployed API
scripts/refresh_prices.py  Regenerate prices.json from the public AWS Price List
.github/workflows/         CI (lint, tests, synth) and keyless OIDC deploy
tests/                     pytest suite: engine, handlers (moto), CDK assertions
```

## Quick start

### 1. Run it locally (no AWS account)

```bash
make install
make test
make plan
```

For live grid data, get a free [Electricity Maps](https://www.electricitymaps.com/) API key:

```bash
ELECTRICITYMAPS_API_KEY=your-key .venv/bin/python scripts/plan_locally.py --runtime 90
```

### 2. Deploy to AWS

You need the AWS CLI configured with credentials, and Node.js for the CDK CLI.

```bash
# Optional: email alerts and an AWS Budget alarm
# edit cdk.json → "alert_email": "you@example.com"

make bootstrap   # once per account and region
make deploy      # 3 worker regions + control plane
```

To use live carbon data in AWS, store the key and switch the provider:

```bash
aws ssm put-parameter --region us-east-1 --type SecureString \
  --name /gridshift/electricitymaps/api-key --value your-key
# cdk.json → "carbon_provider": "electricitymaps", then: make deploy
```

#### Network modes

Set `network_mode` in `cdk.json`:

| Mode | Network | Images | Extra cost |
|---|---|---|---|
| `public` (default) | Public subnets, no inbound rules, no NAT | Pulled from ECR Public | None |
| `private` | Isolated subnets, no internet route at all | ECR pull-through cache in each region | VPC endpoints, about $0.01/hour each per AZ |

#### Keyless deploys from GitHub Actions

`make deploy` also creates `GridShift-GitHubDeploy`, an IAM role that GitHub Actions can assume
through OIDC. It works only for jobs running in this repo's `production` environment, so no AWS
keys are stored in GitHub. After the first deploy:

1. In the GitHub repo settings, create an environment named `production`. Optionally add required reviewers.
2. Add a repository variable `AWS_DEPLOY_ROLE_ARN` set to the stack's `DeployRoleArn` output.
3. Run the **deploy** workflow from the Actions tab.

If the account already has a GitHub OIDC provider, set `github_oidc_provider_arn` in `cdk.json` to reuse it.

### 3. Submit a job

```bash
.venv/bin/python scripts/gridshift_cli.py submit examples/job.json
.venv/bin/python scripts/gridshift_cli.py status <job_id>
```

### 4. Tear down

```bash
make destroy
```

## Job spec

| Field | Required | Description |
|---|---|---|
| `name` | yes | Label for the job |
| `command` | yes | Container command, e.g. `["python", "-c", "print(1)"]` |
| `est_runtime_minutes` | yes | Estimated runtime (1–1440) |
| `deadline` | yes | ISO-8601 with timezone. The job must finish by then. |
| `vcpu`, `memory_gb` | no | A valid Fargate size (default 0.25 vCPU / 0.5 GB) |
| `data_region` | no | Where the input data lives (default: home region) |
| `input_data_gb` | no | Size of input data, used for data-gravity costs |
| `allowed_regions` | no | Data-residency allow list |
| `max_cost_overhead_pct` | no | Max extra cost vs. running now at home (default 25) |

## Cost

At rest it costs almost nothing. Everything is serverless or pay-per-use, there are no NAT
gateways, and logs and outputs expire. A handful of demo jobs costs cents. Set `alert_email`
to get an AWS Budget alarm at 80% of `monthly_budget_usd`.

## Limitations

- Fargate Spot prices aren't published in the Price List because they change over time.
  They are modelled as a 70% discount on real on-demand prices.
- Static grid intensities are rough annual averages, used only when live forecasts are missing.
- The energy model uses a fixed 50% CPU utilisation. Measuring real utilisation is a roadmap item.
- Jobs use a public base image. Custom images need to exist in every worker region.
