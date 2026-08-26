# Bedrock Budget Guard

Tracks **daily (UTC) Bedrock spend in US dollars per project**, alerts when
usage crosses thresholds, and **pauses** Bedrock for over-budget projects by
attaching an IAM Deny. When the budget day resets—or you turn enforcement
off—access is restored.

Projects come from the IAM tag `project` on workload roles. See
[DESIGN.md](DESIGN.md) for architecture and trade-offs.

## Local demo (Docker)

Full stack: ministack emulator + seed + generator + guard.

```bash
cp .env.example .env
make up
make logs-guard   # STATUS / ALERT / BLOCKED / UNBLOCKED
```

```bash
make down    # also removes the guard state volume
make test
```

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
  -v "$PWD/budget-guard/config.aws.yaml:/app/config.yaml:ro" \
  -v budget-guard-aws-state:/app/state \
  bedrock-budget-guard
```

Tear down:

```bash
make aws-down
```

### Safe test checklist

1. `enforce: false` for all projects → watch `STATUS` / spend climb with real traffic.
2. Confirm unknown `modelId`s only produce `WARN` (add prices; we never invent them).
3. Flip `enforce: true` on one low-budget test project → expect `BLOCKED` and an
   inline policy `BudgetGuardDenyBedrock` on that project’s roles.
4. Set `enforce: false` again → `UNBLOCKED` within one poll.

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

## Layout

| Path | Role |
|---|---|
| `budget-guard/` | Guard daemon, local config, unit tests |
| `budget-guard/Dockerfile` | Image for **local demo** Compose |
| `Dockerfile` | Standalone / **real AWS** image |
| `docker-compose.yaml` | Local demo stack |
| `docker-compose.aws.yaml` | Guard only → real AWS |
| `deploy/iam-policy.json` | IAM permissions for the guard |
| `seed/` / `generator/` | Local demo only |
| `DESIGN.md` | Product design and trade-offs |
