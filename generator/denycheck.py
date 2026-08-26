"""IAM invocation-access evaluation for the bedrock-simulator.

A budget tool blocks a role from invoking Bedrock via IAM: either by
attaching a policy whose statements Deny invocation, or by removing
whatever Allow made invocation possible. Real AWS would then reject
the workload's calls (explicit deny, or implicit deny once no allow
remains), so no invocation logs would be written. ministack never
evaluates IAM, so the generator evaluates just enough itself: a role
is blocked when it carries an effective Deny on invocation OR no
longer has any Allow covering invocation. Blocked roles' events are
suppressed, and traffic resumes once access is restored.

Name-agnostic on purpose: whatever the policies are called, only their
statements matter. Fail-open on purpose: an IAM read error must not
fake a successful block.
"""
import json
import logging

logger = logging.getLogger("bedrock-simulator")

_INVOKE_MATCHES = (
    "*",
    "bedrock:*",
    "bedrock:invokemodel",
    "bedrock:invokemodelwithresponsestream",
    "bedrock:converse",
    "bedrock:conversestream",
)


def _action_covers_invoke(action) -> bool:
    if isinstance(action, str):
        action = [action]
    if not isinstance(action, list):
        return False
    return any(isinstance(a, str) and a.lower() in _INVOKE_MATCHES for a in action)


def _statements(doc) -> list:
    if not isinstance(doc, dict):
        return []
    stmts = doc.get("Statement")
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        return []
    return [s for s in stmts if isinstance(s, dict)]


def _doc_has_invoke_effect(doc, effect: str) -> bool:
    for stmt in _statements(doc):
        if stmt.get("Effect") != effect:
            continue
        if _action_covers_invoke(stmt.get("Action")):
            return True
    return False


def is_denying_doc(doc) -> bool:
    """True when any statement is an Effect=Deny whose Action covers
    Bedrock invocation (directly or via wildcard). NotAction statements
    never match: treating them as denying would suppress traffic on
    policies that explicitly exempt invocation."""
    return _doc_has_invoke_effect(doc, "Deny")


def is_allowing_doc(doc) -> bool:
    """True when any statement is an Effect=Allow whose Action covers
    Bedrock invocation. Used for the implicit-deny half of evaluation:
    a role with no allow left cannot invoke."""
    return _doc_has_invoke_effect(doc, "Allow")


def _as_doc(raw):
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _managed_policy_docs(iam, role_name):
    docs = []
    for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
        pol = iam.get_policy(PolicyArn=p["PolicyArn"])["Policy"]
        ver = iam.get_policy_version(
            PolicyArn=p["PolicyArn"], VersionId=pol["DefaultVersionId"],
        )["PolicyVersion"]["Document"]
        docs.append(_as_doc(ver))
    return docs


def _inline_policy_docs(iam, role_name):
    docs = []
    for name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        doc = iam.get_role_policy(
            RoleName=role_name, PolicyName=name,
        )["PolicyDocument"]
        docs.append(_as_doc(doc))
    return docs


def role_is_blocked(iam, role_name: str) -> bool:
    """True when the role cannot invoke Bedrock as IAM would evaluate
    it: an explicit Deny in any attached managed or inline policy, OR
    no Allow covering invocation left anywhere (implicit deny).
    Fail-open: any error logs a warning and returns False."""
    try:
        docs = _managed_policy_docs(iam, role_name) + _inline_policy_docs(iam, role_name)
        if any(is_denying_doc(d) for d in docs):
            return True
        return not any(is_allowing_doc(d) for d in docs)
    except Exception as exc:
        logger.warning(
            "IAM block check failed for role %s (%s); emitting anyway", role_name, exc,
        )
        return False
