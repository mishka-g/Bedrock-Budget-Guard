# Deploy Bedrock Budget Guard

## IAM (required for real AWS)

Attach [`iam-policy.json`](iam-policy.json) to the IAM user/role that runs
the guard.

The Deny-management statement only applies to roles that already have a
`project` tag (`Null=false`). Scope the log-group ARN to your
account/region before production use.

```bash
aws iam create-policy \
  --policy-name BedrockBudgetGuard \
  --policy-document file://deploy/iam-policy.json
```

Tag workload roles:

```bash
aws iam tag-role --role-name MyAppRole --tags Key=project,Value=my-team
```

On EKS, annotate the `budget-guard` ServiceAccount with the role ARN
(IRSA) instead of putting access keys in a Secret.

## Kubernetes

Two replicas: one leader polls CloudWatch + IAM; the other is standby and
still serves `/metrics` and `/healthz`. Compact state lives in ConfigMap
`budget-guard-state` (no DynamoDB). Leader election uses Lease
`budget-guard`.

```bash
# from repo root
docker build -t bedrock-budget-guard .
# load into the cluster (kind/minikube) or push to your registry and
# patch deploy/k8s/deployment.yaml image:
kubectl apply -k deploy/k8s
```

Then:

1. Edit ConfigMap `budget-guard-config` (`projects`, `pricing_per_million_usd`,
   `log_group`). The file is mounted as a directory so hot-reload works.
2. Start with `enforce: false` until `/status` spend looks right.
3. Optional: `kubectl apply -f deploy/k8s/servicemonitor.yaml` if you use
   Prometheus Operator.
4. Import [`grafana/budget-guard.json`](grafana/budget-guard.json) into Grafana.

Env of note:

| Variable | Default | Meaning |
|---|---|---|
| `BUDGET_GUARD_STATE_BACKEND` | `file` locally, `configmap` in the Deployment | Where compact state is stored |
| `BUDGET_GUARD_STATE_CONFIGMAP` | `budget-guard-state` | ConfigMap name |
| `BUDGET_GUARD_LEASE_NAME` | `budget-guard` | Lease name |
| `BUDGET_GUARD_LEADER_ELECTION` | auto-on when `KUBERNETES_SERVICE_HOST` is set | Set `false` to disable |
| `BUDGET_GUARD_HTTP_PORT` | `8080` | `/metrics` `/healthz` `/readyz` `/status` |
| `POD_NAME` / `POD_NAMESPACE` | downward API | Lease identity |

## Grafana / SRE alerts

FinOps panels already filter `budget_guard_is_leader == 1`. Suggested
Prometheus alert rules (not shipped as YAML):

- `max(budget_guard_is_leader) == 0` for 2m — no leader
- `rate(budget_guard_log_fetch_errors_total[5m]) > 0` for 5m — fail-open,
  spend frozen
- `rate(budget_guard_iam_put_failures_total[5m]) > 0` — wanted to block,
  could not
- `budget_guard_watermark_lag_seconds > 120` — falling behind CloudWatch
- `up{job="budget-guard"} == 0` — process down

Fail-open is product policy: a Logs outage does **not** Deny every
project. Page the controller instead.
