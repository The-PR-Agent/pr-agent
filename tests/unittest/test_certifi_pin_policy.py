"""Guard for the security-pin policy declared above ``dependencies`` in pyproject.toml.

certifi is the CA trust store every outbound HTTPS call in pr-agent ultimately
validates against. An ``==`` pin freezes that bundle for pip consumers, who then
cannot pick up a rebuilt bundle -- one that drops a distrusted root, say -- without
overriding our requirement. The policy comment in pyproject.toml says
security-sensitive packages declare ranges for exactly this reason (#2523, #3194);
this test is what keeps the comment honest, since nothing else checks it.

This asserts the declared requirement only. Exact versions for this repo's own
builds still come from uv.lock, so reproducibility is unaffected.
"""
import tomllib

CERTIFI_POLICY = (
    "certifi must declare a floor (>=), not an == pin: it is the CA trust store, and an exact "
    "pin leaves pip consumers stuck on a stale certificate bundle. Exact versions for this "
    "repo's own builds come from uv.lock. See the comment above [project].dependencies."
)


def _certifi_requirement(pytestconfig):
    with open(pytestconfig.inipath, "rb") as f:
        dependencies = tomllib.load(f)["project"]["dependencies"]

    matches = [dep for dep in dependencies if dep.lower().startswith("certifi")]
    assert len(matches) == 1, f"expected exactly one certifi requirement, found {matches!r}"
    return matches[0]


def test_certifi_is_not_exact_pinned(pytestconfig):
    requirement = _certifi_requirement(pytestconfig)
    assert "==" not in requirement, f"{requirement!r}: {CERTIFI_POLICY}"
    assert ">=" in requirement, f"{requirement!r}: {CERTIFI_POLICY}"
