# Bedrock Budget Guard

Tracks **daily (UTC) Bedrock spend in US dollars per project**, alerts when
usage crosses thresholds, and **pauses** Bedrock for over-budget projects by
attaching an IAM Deny. When the budget day resets—or you turn enforcement
off—access is restored.

Projects come from the IAM tag `project` on workload roles. See
[DESIGN.md](DESIGN.md) for architecture and [ROADMAP.md](ROADMAP.md) for
what to build next.

## Local demo (Docker)

Full stack: ministack emulator + seed + generator + guard.

```bash
cp .env.example .env
make up
make logs-guard   # STATUS / ALERT / BLOCKED / UNBLOCKED
# curl http://127.0.0.1:8080/status   # JSON spend vs budget
# curl http://127.0.0.1:8080/metrics
```

```bash
make down    # also removes the guard state volume
make test        # pytest via Compose images
make test-unit   # pytest on the host (what CI runs)
```

Pull requests and pushes to `main` run GitHub Actions: unit tests and
security checks (pip-audit, bandit, gitleaks, Checkov), then a Docker
image build and Trivy scan. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### Quick verify

1. Within ~2–3 minutes: `ALERT` at 80% and 100% for **alpha**, then
   `BLOCKED` for `proj-alpha-app` and `proj-alpha-batch`.
2. Generator logs show those roles suppressed; beta / shared keep writing.
3. Set `projects.alpha.enforce: false` in [`budget-guard/config.yaml`](budget-guard/config.yaml)
   → `UNBLOCKED` within one poll.

## Real AWS (Docker)

Guard-only image/compose — **separate** from the local demo. Use a test
account first; start with `enforce: false` until STATUS looks right.

### 1. AWS prerequisites

1. Enable [Bedrock model invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) to CloudWatch.
2. Tag workload roles: `project=<name>` (must match keys under `projects:` in config).
3. Create an IAM user/role for the guard and attach [`deploy/iam-policy.json`](deploy/iam-policy.json)
   (see [`deploy/README.md`](deploy/README.md)). Adjust the log-group ARN to your
   account/region before production.

### 2. Config and credentials

```bash
cp .env.aws.example .env.aws
cp budget-guard/config.aws.example.yaml budget-guard/config.aws.yaml
```

Edit `.env.aws` (`AWS_DEFAULT_REGION` + keys, or use a mounted `~/.aws` profile).
Edit `budget-guard/config.aws.yaml`: `log_group`, `projects`, `pricing_per_million_usd`
for models you actually invoke. Do **not** set `AWS_ENDPOINT_URL`.

### 3. Run

```bash
make aws-up
make aws-logs
```

Same thing with Compose directly:

```bash
docker compose -f docker-compose.aws.yaml up -d --build
docker compose -f docker-compose.aws.yaml logs -f budget-guard
```

Or build/run the image yourself:

```bash
docker build -t bedrock-budget-guard .
docker run --rm \
  --env-file .env.aws \
  -e AWS_ENDPOINT_URL= \
  -e BUDGET_GUARD_CONFIG=/app/config.yaml \
  -e BUDGET_GUARD_STATE=/app/state/state.json \
  -p 8080:8080 \
  -v "$PWD/budget-guard/config.aws.yaml:/app/config.yaml:ro" \
  -v budget-guard-aws-state:/app/state \
  bedrock-budget-guard
```

Tear down:

```bash
make aws-down    # stops containers; keeps the state volume
```

### Safe test checklist

1. `enforce: false` for all projects → watch `STATUS` / spend climb with real traffic.
2. Confirm unknown `modelId`s only produce `WARN` (add prices; we never invent them).
3. Flip `enforce: true` on one low-budget test project → expect `BLOCKED` and an
   inline policy `BudgetGuardDenyBedrock` on that project’s roles.
4. Set `enforce: false` again → `UNBLOCKED` within one poll.

## Kubernetes

Production: two replicas, Lease leader election, compact state in a
ConfigMap (not DynamoDB). See [`deploy/README.md`](deploy/README.md).

```bash
docker build -t bedrock-budget-guard .
kubectl apply -k deploy/k8s
```

Edit the `budget-guard-config` ConfigMap (projects, prices), attach IRSA
or a node role using [`deploy/iam-policy.json`](deploy/iam-policy.json),
import [`deploy/grafana/budget-guard.json`](deploy/grafana/budget-guard.json).

HTTP on every pod (standby and leader): `/healthz`, `/readyz`, `/metrics`,
`/status`. Grafana FinOps panels use `budget_guard_is_leader == 1`.

**Fail-open:** if CloudWatch Logs cannot be read, last spend is kept and
no extra Deny is attached. Alert on `budget_guard_log_fetch_errors_total`
and `budget_guard_iam_put_failures_total`.

## Config

| Mode | File |
|---|---|
| Local demo | [`budget-guard/config.yaml`](budget-guard/config.yaml) |
| Real AWS | `budget-guard/config.aws.yaml` (from the example; gitignored) |

Both are mounted read-only and **reloaded every poll** — no rebuild for normal edits.

| What you want | What to edit |
|---|---|
| Daily money limit for a project | `projects.<name>.budget_usd` — **US dollars**, not tokens |
| Stop auto-blocking a project today | `projects.<name>.enforce: false` |
| Raise the limit so blocking lifts | Increase `budget_usd` above today’s spend |
| Warn earlier / later | `alert_thresholds` (e.g. `[0.8, 1.0]`) |
| How often it checks | `poll_interval_seconds` |
| Token → dollar rates | `pricing_per_million_usd` |
| Slack notifications | `alerts.slack` (+ optional `SLACK_WEBHOOK_URL`) |

### Slack

1. Create a Slack [Incoming Webhook](https://api.slack.com/messaging/webhooks).
2. In config set `alerts.slack.enabled: true` and either:
   - export `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...`, or
   - set `alerts.slack.webhook_url` (env wins if both are set).
3. Default `events`: `ALERT`, `BLOCKED`, `UNBLOCKED` (not `STATUS`).

Stdout always continues; Slack failures are logged as `WARN` and ignored.

## Layout

| Path | Role |
|---|---|
| `budget-guard/` | Guard daemon, local config, unit tests |
| `budget-guard/Dockerfile` | Image for **local demo** Compose |
| `Dockerfile` | Standalone / **real AWS** / **Kubernetes** image (no baked config) |
| `docker-compose.yaml` | Local demo stack |
| `docker-compose.aws.yaml` | Guard only → real AWS |
| `deploy/k8s/` | Kubernetes manifests (2 replicas, Lease, ConfigMap state) |
| `deploy/grafana/budget-guard.json` | Grafana dashboard |
| `deploy/iam-policy.json` | IAM permissions for the guard |
| `.github/workflows/ci.yml` | PR/main CI: tests, security, image build |
| `seed/` / `generator/` | Local demo only |
| `DESIGN.md` | Product design and trade-offs |
| `ROADMAP.md` | Priorities for what to build next |
