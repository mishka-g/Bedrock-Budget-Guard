# Attach this policy to the IAM user/role that runs Bedrock Budget Guard.
#
# The Deny-management statement only applies to roles that already have a
# `project` tag (Null=false). Scope the log-group ARN to your account/region
# before production use.
#
# Example:
#   aws iam create-policy \
#     --policy-name BedrockBudgetGuard \
#     --policy-document file://deploy/iam-policy.json
#
# Then attach to your user/role and tag workload roles:
#   aws iam tag-role --role-name MyAppRole --tags Key=project,Value=my-team

See `iam-policy.json` in this folder.
