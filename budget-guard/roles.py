"""IAM role discovery and ARN → role name helpers."""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("budget-guard")

# arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/SESSION
# also accept iam::ACCOUNT:role/ROLE_NAME
_ASSUMED_ROLE_RE = re.compile(
    r"arn:aws:sts::\d+:assumed-role/([^/]+)/",
)
_IAM_ROLE_RE = re.compile(
    r"arn:aws:iam::\d+:role/([^/]+)$",
)


def role_name_from_arn(arn: str | None) -> str | None:
    """Extract IAM role name from a Bedrock identity ARN."""
    if not arn or not isinstance(arn, str):
        return None
    m = _ASSUMED_ROLE_RE.search(arn)
    if m:
        return m.group(1)
    m = _IAM_ROLE_RE.search(arn)
    if m:
        return m.group(1)
    return None


def load_role_project_map(iam) -> dict[str, str]:
    """Map role name → project tag value for every IAM role that has project=.

    Roles without a project tag are omitted.
    """
    mapping: dict[str, str] = {}
    try:
        paginator = iam.get_paginator("list_roles")
        role_names: list[str] = []
        for page in paginator.paginate():
            for role in page.get("Roles", []):
                name = role.get("RoleName")
                if name:
                    role_names.append(name)
    except Exception:
        # ministack may not support pagination; fall back to a single call.
        try:
            role_names = [r["RoleName"] for r in iam.list_roles().get("Roles", [])]
        except Exception as exc:
            logger.warning("Failed to list IAM roles: %s", exc)
            return mapping

    for name in role_names:
        try:
            tags = {
                t["Key"]: t["Value"]
                for t in iam.list_role_tags(RoleName=name).get("Tags", [])
            }
        except Exception as exc:
            logger.warning("Failed to list tags for role %s: %s", name, exc)
            continue
        project = tags.get("project")
        if project:
            mapping[name] = project
    return mapping


def roles_for_project(role_map: dict[str, str], project: str) -> list[str]:
    """Sorted role names tagged with the given project."""
    return sorted(name for name, proj in role_map.items() if proj == project)


def project_for_message(
    message: dict[str, Any], role_map: dict[str, str],
) -> tuple[str | None, str | None]:
    """Return (role_name, project) from an invocation message, or (None, None)."""
    identity = message.get("identity") or {}
    if not isinstance(identity, dict):
        return None, None
    arn = identity.get("arn")
    role = role_name_from_arn(arn)
    if not role:
        return None, None
    return role, role_map.get(role)
