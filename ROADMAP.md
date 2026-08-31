# Roadmap

Priority for what to build next. v1 of the product is done (track / alert /
block, local demo, real AWS, Kubernetes HA, metrics, CI). What remains is
**ship it, then scale it, then make FinOps less manual**.

See [DESIGN.md](DESIGN.md) for architecture and trade-offs.

## Done

Do not re-plan these.

| Area | What we have |
|---|---|
| Product | UTC-day $ budgets, thresholds, IAM Deny, hot-reload, Slack |
| HA | 2 replicas, Lease, ConfigMap state, fail-open |
| Observability | `/metrics` `/status` `/healthz`, Grafana dashboard JSON |
| CI | tests + pip-audit / bandit / gitleaks / Checkov, then image build + Trivy |
| Packaging | non-root prod image, OpenSSL refresh at build time |

## P0 — Make it runnable in production

Nothing here changes the algorithm. It unblocks a first cluster.

1. **Publish the image.** CI currently builds and discards the image. Push
   `ghcr.io/…/bedrock-budget-guard:<sha>` (and `:latest` on `main`). Point
   [`deploy/k8s/deployment.yaml`](deploy/k8s/deployment.yaml) at that.
2. **First EKS install.** IRSA on the ServiceAccount, **scope**
   [`deploy/iam-policy.json`](deploy/iam-policy.json) to account / region /
   log-group, `enforce: false` until `/status` looks right, then one test
   project to `true`.
3. **Wire Grafana + SRE alerts.** Import
   [`deploy/grafana/budget-guard.json`](deploy/grafana/budget-guard.json).
   Add the alert rules listed in [`deploy/README.md`](deploy/README.md)
   (no leader, fetch errors, IAM put failures, watermark lag, `up==0`).
   Fail-open is useless if nobody pages.
4. **Ship PrometheusRule YAML** (optional but small) so alerts are gitops,
   not a wiki.

**Exit:** one account, one region, a handful of projects; on-call can see
spend and know if the guard is dead.

## P1 — Survive 200+ projects (scale, not more HA)

Replicas do not shard work. Fix the **single leader’s AWS chatter**.

1. **Stop `list_roles` + `list_role_tags` on every role.** Use the Resource
   Groups Tagging API (`tag:GetResources` with `project=*`) or “roles seen
   in logs + cache.” This is the first wall in a busy account.
2. **IAM only on transition.** Put Deny when a project *becomes* blocked;
   lift when it *becomes* unblocked. Do not `put_role_policy` /
   `delete_role_policy` every 15s. Slow background reconcile (for example
   every 5–10 min) for new roles.
3. **Watch `poll_duration_seconds` and `watermark_lag_seconds`** after
   (1) and (2). Only then consider log ingest changes (subscription filter
   / SQS) if FilterLogEvents cannot keep up.

**Exit:** 200 tagged projects, most under budget, poll stays well under 15s.

## P2 — FinOps accuracy and less YAML babysitting

1. **Unknown `modelId` is a first-class gap.** Already a metric; add an ops
   path: “add this price or spend is undercounted.”
2. **Per-region / Price List sync.** Price by region (or sync from the AWS
   Price List / Bulk API) so `eu-west-1` vs US list prices stay accurate.
   Config remains a manual override; sync must fail soft if the price API
   is unreachable.
3. **Config as data.** Many projects in one ConfigMap is fine at 200; if
   FinOps wants self-service, a tiny API or git PR template beats a rewrite.

## P3 — Nice to have

Do not start until P0/P1 are boring.

| Item | Notes |
|---|---|
| PagerDuty, email, or richer routing by project | Same once-per-threshold semantics as Slack. Stdout stays useful for local review. |
| Dependabot | CI already fails on known CVEs at PR time |
| Helm | kustomize is enough until you have many clusters |
| DynamoDB / multi-leader | ConfigMap + Lease is the right size |
| Fail-closed | Explicit product change; v1 is fail-open |
| Full-day rescan on failover | Small gap on purpose |
| Smoke vs ministack in CI | Slow; unit tests + image build already gate PRs |
| Multi-account / multi-region sharding | One Deployment per account/region if needed — ops, not a new architecture |

## Suggested next slices

1. Push image to GHCR (or ECR) from the existing build job.
2. IRSA + scoped IAM + first EKS apply (`enforce: false` → `true` on one team).
3. Prometheus alerts from [`deploy/README.md`](deploy/README.md).
4. Tagged-role discovery (kill account-wide `ListRoleTags`).
5. IAM on state change only.
6. Price List sync only if Finance complains about region rates.
