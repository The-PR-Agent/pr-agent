"""Labelled defect corpus for measuring review recall.

Two kinds of item, both cheap to label:

* ``reverted-fix`` - the reverse of a real fix commit in this repository. The defect is exactly
  what the fix deleted, and the commit message states the consequence, so the label is the
  project's own words rather than a guess. Only ``pr_agent/`` is reversed: a PR that
  reintroduced a bug would not also ship the tests that catch it.
* ``mutant`` - a hand-written single-site defect in a plausible file that does **not** exist in
  this checkout. Non-existent paths matter: PlainDiffGitProvider enriches from the working tree
  when the path resolves (``plain_diff_provider.py:78``), which would replace a mutant with the
  real file and silently invalidate the score.

Recall over this corpus measures *the classes seeded here and nothing else*. See ACCURACY_PLAN.md
section 5 - a model can score well on mechanical single-site defects and still miss the
cross-module reasoning failures that small local models are worst at.
"""

import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeededDefect:
    id: str
    defect_class: str
    summary: str
    #: Files the defect lives in. A finding elsewhere is not a hit, however well worded.
    files: tuple[str, ...]
    #: Lowercase substrings. A finding on a matching file that contains any one of these counts.
    #: Keep them about the *defect*, not about the fix's wording, or the score measures echoing.
    signals: tuple[str, ...]
    source: str
    #: True when the reversed fix also removed an explanatory comment, handing the model the
    #: answer in the diff itself. Scored separately - these inflate recall.
    leaks_rationale: bool = False
    #: Literal unified diff; empty for reverted-fix items, which are generated from git.
    diff_text: str = field(default="", repr=False)


def reverted_fix_diff(sha: str, repo_root: str = ".") -> str:
    """Reverse a fix commit into a PR that reintroduces its bug (source files only)."""
    return subprocess.run(
        ["git", "-C", repo_root, "diff", sha, f"{sha}^", "--", "pr_agent/"],
        capture_output=True, text=True, check=True,
    ).stdout


REVERTED_FIXES = (
    SeededDefect(
        id="revert-a6484241",
        defect_class="partial-result-treated-as-complete",
        summary=(
            "A review whose chunks partly failed is treated as complete, so findings that the "
            "failed chunks would have reported get marked resolved."
        ),
        files=("pr_agent/tools/pr_reviewer.py",),
        signals=("failed chunk", "partial", "resolve", "incomplete"),
        source="reverted-fix:a6484241",
        leaks_rationale=True,  # the fix added a comment naming the bug; reversing it removes that
    ),
    SeededDefect(
        id="revert-d5c15c49",
        defect_class="dropped-argument",
        summary="Persistent comment links are lost because the link fields are no longer passed.",
        files=("pr_agent/git_providers/bitbucket_provider.py",),
        signals=("link", "url", "persistent", "comment"),
        source="reverted-fix:d5c15c49",
    ),
    SeededDefect(
        id="revert-00b8c9d4",
        defect_class="silent-failure",
        summary=(
            "Code-suggestion publication failures are swallowed, so a provider that published "
            "nothing reports success."
        ),
        files=(
            "pr_agent/git_providers/codecommit_provider.py",
            "pr_agent/git_providers/gerrit_provider.py",
        ),
        signals=("silent", "swallow", "failure", "return", "success", "error"),
        source="reverted-fix:00b8c9d4",
    ),
    SeededDefect(
        id="revert-8bd15328",
        defect_class="silent-failure",
        summary=(
            "When every inline suggestion falls back and the fallback also fails, the total "
            "failure is not reported."
        ),
        files=("pr_agent/git_providers/github_provider.py",),
        signals=("fallback", "fail", "report", "silent"),
        source="reverted-fix:8bd15328",
    ),
)


def _diff(path: str, start: int, section: str, body: str) -> str:
    """Build a one-hunk unified diff, deriving the @@ counts from the body.

    Hand-written counts are the easiest thing to get wrong here, and unidiff rejects the whole
    diff when they disagree with the body ("Hunk is shorter than expected"), so derive them.
    """
    lines = body.splitlines()
    old = sum(1 for ln in lines if ln[:1] in (" ", "-"))
    new = sum(1 for ln in lines if ln[:1] in (" ", "+"))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},{old} +{start},{new} @@ {section}\n{body}"
    )


MUTANTS = (
    SeededDefect(
        id="mutant-off-by-one",
        defect_class="boundary",
        summary="Retry loop runs one attempt short; the last configured attempt never happens.",
        files=("svc/net/retry.py",),
        signals=("off-by-one", "off by one", "boundary", "one fewer", "last attempt",
                 "max_attempts", "range"),
        source="mutant",
        diff_text=_diff("svc/net/retry.py", 12, "def send_with_retry(request, max_attempts):", """     last_error = None
-    for attempt in range(max_attempts):
+    for attempt in range(max_attempts - 1):
         try:
             return transport.send(request)
         except TransportError as e:
             last_error = e
"""),
    ),
    SeededDefect(
        id="mutant-swapped-args",
        defect_class="swapped-arguments",
        summary="Arguments are transposed, so the transfer moves money in the wrong direction.",
        files=("svc/billing/transfer.py",),
        signals=("swap", "transpose", "wrong order", "reversed", "src", "dst", "direction"),
        source="mutant",
        diff_text=_diff("svc/billing/transfer.py", 30, "def settle(order):", """     amount = order.total_cents
-    ledger.move(src=order.customer_account, dst=order.merchant_account, cents=amount)
+    ledger.move(src=order.merchant_account, dst=order.customer_account, cents=amount)
     order.mark_settled()
"""),
    ),
    SeededDefect(
        id="mutant-dropped-none-check",
        defect_class="null-dereference",
        summary="The None guard is gone, so a missing profile raises AttributeError.",
        files=("svc/users/profile.py",),
        signals=("none", "null", "attributeerror", "guard", "missing", "not exist"),
        source="mutant",
        diff_text=_diff(
            "svc/users/profile.py", 18, "def display_name(user_id):",
            """     profile = store.get_profile(user_id)
-    if profile is None:
-        return DEFAULT_NAME
     return profile.full_name.strip()
"""),
    ),
    SeededDefect(
        id="mutant-dropped-await",
        defect_class="missing-await",
        summary="The write is never awaited, so the handler returns before the record is saved.",
        files=("svc/api/orders.py",),
        signals=("await", "coroutine", "not awaited", "never executed", "async"),
        source="mutant",
        diff_text=_diff(
            "svc/api/orders.py", 44, "async def create_order(payload):",
            """     order = build_order(payload)
-    await repository.persist(order)
+    repository.persist(order)
     return {"id": order.id}
"""),
    ),
    SeededDefect(
        id="mutant-widened-except",
        defect_class="silent-failure",
        summary="A bare except swallows every error, so a failed charge is reported as success.",
        files=("svc/billing/charge.py",),
        signals=("bare except", "broad except", "swallow", "silent", "exception", "hides"),
        source="mutant",
        diff_text=_diff("svc/billing/charge.py", 55, "def charge(card, cents):", """     try:
         return gateway.charge(card, cents)
-    except GatewayDeclined:
-        return ChargeResult(ok=False, reason="declined")
+    except Exception:
+        return ChargeResult(ok=True, reason="")
"""),
    ),
    SeededDefect(
        id="mutant-removed-authz",
        defect_class="missing-authorization",
        summary="The ownership check is gone, so any authenticated user can delete any document.",
        files=("svc/api/documents.py",),
        signals=("authorization", "authz", "permission", "ownership", "access control",
                 "any user", "idor"),
        source="mutant",
        diff_text=_diff(
            "svc/api/documents.py", 71, "def delete_document(request, doc_id):",
            """     doc = store.get(doc_id)
-    if doc.owner_id != request.user.id:
-        raise Forbidden()
     store.delete(doc_id)
     return Response(status=204)
"""),
    ),
    SeededDefect(
        id="mutant-flipped-comparison",
        defect_class="boundary",
        summary="The expiry comparison is flipped, so expired tokens are accepted as valid.",
        files=("svc/auth/token.py",),
        signals=("expiry", "expired", "comparison", "flipped", "inverted", "accept", "valid"),
        source="mutant",
        diff_text=_diff(
            "svc/auth/token.py", 9, "def is_valid(token, now):",
            """     if token.signature != expected_signature(token):
         return False
-    return token.expires_at > now
+    return token.expires_at < now
"""),
    ),
)

ALL_DEFECTS = REVERTED_FIXES + MUTANTS
