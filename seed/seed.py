"""One-shot bootstrap of the fake AWS account state in ministack.

Idempotent: every create tolerates already-exists, so re-running
docker compose never fails or duplicates. Anything else raises,
giving the container a non-zero exit that compose surfaces.
"""
import json
import os

import boto3

LOG_GROUP = "/aws/bedrock/modelinvocations"

ROLES = [
    {"name": "proj-alpha-app", "tags": {"project": "alpha", "platform": "k8s"}},
    {"name": "proj-alpha-batch", "tags": {"project": "alpha", "platform": "k8s"}},
    {"name": "proj-beta-app", "tags": {"project": "beta", "platform": "k8s"}},
    {"name": "ai-developers", "tags": {"project": "shared", "platform": "local"}},
]

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

# The Allow policy that makes each role able to invoke Bedrock models,
# mirroring how real workload roles are granted access. Budget
# enforcement is expected to work the way IAM actually evaluates:
# an explicit Deny (attached by the candidate's tool) overrides this
# Allow. The Allow itself must never be detached or edited.
INVOKE_POLICY_NAME = "BedrockInvokeAccess"
INVOKE_POLICY_DOC = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "AllowBedrockInvoke",
        "Effect": "Allow",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        "Resource": "*",
    }],
}


def main() -> None:
    endpoint = os.environ["AWS_ENDPOINT_URL"]
    logs = boto3.client("logs", endpoint_url=endpoint)
    iam = boto3.client("iam", endpoint_url=endpoint)

    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        print(f"seed: created log group {LOG_GROUP}")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"seed: log group {LOG_GROUP} already exists")

    try:
        resp = iam.create_policy(
            PolicyName=INVOKE_POLICY_NAME,
            PolicyDocument=json.dumps(INVOKE_POLICY_DOC),
            Description="Allow invoking Bedrock models; budget tools deny on top of this",
        )
        policy_arn = resp["Policy"]["Arn"]
        print(f"seed: created policy {INVOKE_POLICY_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        account_id = boto3.client(
            "sts", endpoint_url=endpoint,
        ).get_caller_identity()["Account"]
        policy_arn = f"arn:aws:iam::{account_id}:policy/{INVOKE_POLICY_NAME}"
        print(f"seed: policy {INVOKE_POLICY_NAME} already exists")

    for role in ROLES:
        try:
            iam.create_role(
                RoleName=role["name"],
                AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
                Description="seeded workload role for the budget-tool assignment",
            )
            print(f"seed: created role {role['name']}")
        except iam.exceptions.EntityAlreadyExistsException:
            print(f"seed: role {role['name']} already exists")
        # tag_role overwrites existing tag values, so it is naturally idempotent.
        iam.tag_role(
            RoleName=role["name"],
            Tags=[{"Key": k, "Value": v} for k, v in role["tags"].items()],
        )
        # attach_role_policy is a no-op success when already attached.
        iam.attach_role_policy(RoleName=role["name"], PolicyArn=policy_arn)
        print(f"seed: {INVOKE_POLICY_NAME} attached to {role['name']}")

    print("seed: done")


if __name__ == "__main__":
    main()
