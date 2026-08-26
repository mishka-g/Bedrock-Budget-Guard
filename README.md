# Bedrock Budget Guard

Tracks **daily (UTC) Bedrock spend in US dollars per project**, alerts when
usage crosses thresholds, and **pauses** Bedrock for over-budget projects by
attaching an IAM Deny. When the budget day resets—or you turn enforcement
off—access is restored.

Projects come from the IAM tag `project` on workload roles (for example
`alpha`, `beta`, `shared`). See [DESIGN.md](DESIGN.md) for architecture and
trade-offs.

## Local demo (Docker)

The repo includes a ministack emulator, seed IAM roles/log group, a log
generator, and the guard daemon.

```bash
cp .env.example .env
make up
make logs-guard   # STATUS / ALERT / BLOCKED / UNBLOCKED
```

Tear down (also removes the guard state volume):

```bash
make down
```

Unit tests:

```bash
make test
```

### Quick verify

1. Within ~2–3 minutes: `ALERT` at 80% and 100% for **alpha**, then
   `BLOCKED` for `proj-alpha-app` and `proj-alpha-batch`.
2. Generator logs show those roles suppressed; beta / shared keep writing.
3. Set `projects.alpha.enforce: false` in [`budget-guard/config.yaml`](budget-guard/config.yaml)
   → `UNBLOCKED` within one poll.

## Real AWS

The guard talks to CloudWatch Logs and IAM via boto3. For production:

1. Tag workload roles with `project=<name>` matching keys under `projects:` in
   [`budget-guard/config.yaml`](budget-guard/config.yaml).
2. Point `log_group` at your Bedrock model invocation log group (default in
   config: `/aws/bedrock/modelinvocations`).
3. Give the guard identity permission to:
   - `logs:FilterLogEvents` on that log group
   - `iam:ListRoles`, `iam:ListRoleTags`, `iam:GetRolePolicy`,
     `iam:PutRolePolicy`, `iam:DeleteRolePolicy` on the roles it may manage
4. Run without `AWS_ENDPOINT_URL` (standard AWS endpoints). Example:

```bash
cd budget-guard
pip install -r requirements.txt
unset AWS_ENDPOINT_URL
export AWS_DEFAULT_REGION=eu-west-1
# credentials via env, profile, or instance/role
export BUDGET_GUARD_CONFIG=./config.yaml
export BUDGET_GUARD_STATE=./state/state.json
mkdir -p state
python main.py
```

Or run only the guard container against real AWS by mounting config/state and
passing real credentials—omit `AWS_ENDPOINT_URL` from the environment.

Tune `budget_usd`, `pricing_per_million_usd`, and `alert_thresholds` for your
account and region. Demo rates are US-style list prices sized for the local
simulator.

## Config

Edit [`budget-guard/config.yaml`](budget-guard/config.yaml) on the host. In
Compose it is mounted read-only; the process **reloads it every poll**
(default ~15 seconds). No rebuild needed for normal edits.

Spend / watermark / blocked set persist under `/app/state/state.json`
(Compose volume `budget-guard-state`). `make down` wipes that volume.

| What you want | What to edit |
|---|---|
| Daily money limit for a project | `projects.<name>.budget_usd` — **US dollars**, not tokens |
| Stop auto-blocking a project today | `projects.<name>.enforce: false` |
| Raise the limit so blocking lifts | Increase `budget_usd` above today’s spend |
| Warn earlier / later | `alert_thresholds` (e.g. `[0.8, 1.0]`) |
| How often it checks | `poll_interval_seconds` |
| Token → dollar rates | `pricing_per_million_usd` |

Unknown models are skipped with a warning—we never invent a price.

Default demo budgets: **alpha $2/day**, **beta $50/day**, **shared $50/day**.

## Layout

| Path | Role |
|---|---|
| `budget-guard/` | Guard daemon, config, unit tests |
| `seed/` | Creates log group + tagged IAM roles (local demo) |
| `generator/` | Writes Bedrock-style invocation logs (local demo) |
| `docker-compose.yaml` | ministack + seed + generator + guard |
| `DESIGN.md` | Product design and trade-offs |
