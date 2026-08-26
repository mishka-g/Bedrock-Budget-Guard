"""Idempotent IAM Deny attach/lift for Bedrock invoke actions.

NEVER detaches or edits BedrockInvokeAccess — Deny overrides Allow.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("budget-guard")

# Fixed inline policy name; denycheck is name-agnostic (statements matter).
DENY_POLICY_NAME = "BudgetGuardDenyBedrock"

DENY_POLICY_DOC = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "BudgetGuardDenyBedrockInvoke",
        "Effect": "Deny",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        "Resource": "*",
    }],
}


def put_deny(iam, role_name: str) -> bool:
    """Attach (or refresh) the inline Deny. Returns True on success."""
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=DENY_POLICY_NAME,
            PolicyDocument=json.dumps(DENY_POLICY_DOC),
        )
        return True
    except Exception as exc:
        logger.warning("Failed to put Deny on role %s: %s", role_name, exc)
        return False


def delete_deny(iam, role_name: str) -> bool:
    """Remove our inline Deny if present. Idempotent. Returns True if gone."""
    try:
        iam.delete_role_policy(
            RoleName=role_name,
            PolicyName=DENY_POLICY_NAME,
        )
        return True
    except Exception as exc:
        # Already absent is fine (NoSuchEntity / not found).
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchEntity", "NoSuchEntityException"):
            return True
        # Some emulators raise ClientError with different shapes.
        msg = str(exc).lower()
        if "nosuchentity" in msg or "not found" in msg or "cannot find" in msg:
            return True
        logger.warning("Failed to delete Deny on role %s: %s", role_name, exc)
        return False


def block_project_roles(iam, role_names: list[str]) -> list[str]:
    """Put Deny on each role. Returns roles successfully denied."""
    blocked: list[str] = []
    for name in role_names:
        if put_deny(iam, name):
            blocked.append(name)
    return blocked


def unblock_project_roles(iam, role_names: list[str]) -> list[str]:
    """Delete Deny on each role. Returns roles where Deny is gone."""
    lifted: list[str] = []
    for name in role_names:
        if delete_deny(iam, name):
            lifted.append(name)
    return lifted


def role_has_deny(iam, role_name: str) -> bool:
    """True if our managed inline Deny is attached to the role."""
    try:
        iam.get_role_policy(RoleName=role_name, PolicyName=DENY_POLICY_NAME)
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchEntity", "NoSuchEntityException"):
            return False
        msg = str(exc).lower()
        if "nosuchentity" in msg or "not found" in msg or "cannot find" in msg:
            return False
        # Fallback: list inline policy names.
        try:
            names = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
            return DENY_POLICY_NAME in names
        except Exception as inner:
            logger.warning(
                "Failed to check Deny on role %s: %s / %s", role_name, exc, inner,
            )
            return False


def discover_blocked_projects(iam, role_map: dict[str, str]) -> set[str]:
    """Projects that currently have our Deny on at least one tagged role."""
    blocked: set[str] = set()
    for role_name, project in role_map.items():
        if role_has_deny(iam, role_name):
            blocked.add(project)
    return blocked
