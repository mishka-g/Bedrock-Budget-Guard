import denycheck


def deny(action):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Action": action, "Resource": "*"}]}


def test_deny_exact_action():
    assert denycheck.is_denying_doc(deny("bedrock:InvokeModel"))


def test_deny_action_list_mixed():
    assert denycheck.is_denying_doc(deny(["s3:GetObject", "bedrock:Converse"]))


def test_deny_bedrock_wildcard():
    assert denycheck.is_denying_doc(deny("bedrock:*"))


def test_deny_star():
    assert denycheck.is_denying_doc(deny("*"))


def test_case_insensitive():
    assert denycheck.is_denying_doc(deny("BEDROCK:invokemodel"))


def test_allow_never_matches():
    doc = {"Statement": [
        {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": "*"}]}
    assert not denycheck.is_denying_doc(doc)


def test_deny_other_service_does_not_match():
    assert not denycheck.is_denying_doc(deny("s3:*"))


def test_single_statement_dict_form():
    doc = {"Statement": {"Effect": "Deny", "Action": "*"}}
    assert denycheck.is_denying_doc(doc)


def test_notaction_ignored():
    doc = {"Statement": [
        {"Effect": "Deny", "NotAction": "s3:*", "Resource": "*"}]}
    assert not denycheck.is_denying_doc(doc)


def test_garbage_tolerated():
    assert not denycheck.is_denying_doc(None)
    assert not denycheck.is_denying_doc({})
    assert not denycheck.is_denying_doc({"Statement": "nope"})
    assert not denycheck.is_denying_doc({"Statement": [{"Effect": "Deny"}]})


def allow(action):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": action, "Resource": "*"}]}


def test_allow_invoke_detected():
    assert denycheck.is_allowing_doc(allow("bedrock:InvokeModel"))
    assert denycheck.is_allowing_doc(allow(["s3:GetObject", "bedrock:*"]))


def test_allow_other_service_not_detected():
    assert not denycheck.is_allowing_doc(allow("s3:*"))


def test_deny_is_not_an_allow():
    assert not denycheck.is_allowing_doc(deny("bedrock:InvokeModel"))
    assert not denycheck.is_denying_doc(allow("bedrock:InvokeModel"))
