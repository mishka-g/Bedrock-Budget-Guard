"""Environment sanity check (internal, not part of the candidate task).

Hard assertions (non-zero exit on failure):
  1. ministack healthy
  2. seeded roles exist with expected tags; log group exists
  3. log events are flowing (new events appear within a 75s window)

Informational (printed, never fails):
  4. Logs Insights stats query support in the pinned ministack version
"""
import os
import sys
import time
import urllib.request

import boto3

ENDPOINT = os.environ["AWS_ENDPOINT_URL"]
LOG_GROUP = "/aws/bedrock/modelinvocations"
EXPECTED_ROLES = {
    "proj-alpha-app": {"project": "alpha", "platform": "k8s"},
    "proj-alpha-batch": {"project": "alpha", "platform": "k8s"},
    "proj-beta-app": {"project": "beta", "platform": "k8s"},
    "ai-developers": {"project": "shared", "platform": "local"},
}
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        FAILURES.append(name)


def count_events_since(logs, since_ms: int) -> int:
    # Count only a recent window: ministack 1.4.1 caps FilterLogEvents
    # at 1000 events and returns no nextToken, so an all-time count
    # saturates on a long-running stack. A windowed count stays small
    # and correct regardless of total volume.
    total, token = 0, None
    while True:
        kwargs = {"logGroupName": LOG_GROUP, "limit": 1000, "startTime": since_ms}
        if token:
            kwargs["nextToken"] = token
        resp = logs.filter_log_events(**kwargs)
        total += len(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return total


def main() -> None:
    health_url = ENDPOINT.rstrip("/") + "/_ministack/health"
    for _ in range(30):
        try:
            if urllib.request.urlopen(health_url, timeout=3).status == 200:
                break
        except OSError:
            time.sleep(2)
    else:
        check("ministack health", False, health_url)
        sys.exit(1)
    check("ministack health", True)

    iam = boto3.client("iam", endpoint_url=ENDPOINT)
    logs = boto3.client("logs", endpoint_url=ENDPOINT)

    for role, expected_tags in EXPECTED_ROLES.items():
        try:
            tags = {
                t["Key"]: t["Value"]
                for t in iam.list_role_tags(RoleName=role)["Tags"]
            }
            check(f"role {role}", tags == expected_tags, f"tags={tags}")
            attached = {
                p["PolicyName"]
                for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
            }
            check(
                f"role {role} invoke policy",
                "BedrockInvokeAccess" in attached,
                f"attached={sorted(attached)}",
            )
        except Exception as exc:
            check(f"role {role}", False, str(exc))

    try:
        groups = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)["logGroups"]
        check("log group", any(g["logGroupName"] == LOG_GROUP for g in groups))
    except Exception as exc:
        check("log group", False, str(exc))

    try:
        window_start_ms = int(time.time() * 1000)
        print("...   waiting 75s for new events")
        time.sleep(75)
        recent = count_events_since(logs, window_start_ms)
        check("events flowing", recent > 0, f"{recent} events in the last 75s")
    except Exception as exc:
        check("events flowing", False, str(exc))

    # Informational: Logs Insights stats support in this ministack version.
    insights = "unknown"
    try:
        now = int(time.time())
        q = logs.start_query(
            logGroupName=LOG_GROUP,
            startTime=now - 3600,
            endTime=now,
            queryString=(
                "fields input.inputTokenCount as in_tok"
                " | filter ispresent(modelId)"
                " | stats sum(in_tok) as total by modelId"
            ),
        )
        for _ in range(15):
            res = logs.get_query_results(queryId=q["queryId"])
            if res["status"] in ("Complete", "Failed", "Cancelled"):
                break
            time.sleep(2)
        if res["status"] == "Complete" and res.get("results"):
            insights = "WORKS (stats sum by modelId returned rows)"
        else:
            insights = f"status={res['status']} rows={len(res.get('results', []))}"
    except Exception as exc:
        insights = f"error: {exc}"
    print(f"INSIGHTS SUPPORT: {insights}")

    if FAILURES:
        print(f"SMOKE FAILED: {FAILURES}")
        sys.exit(1)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
