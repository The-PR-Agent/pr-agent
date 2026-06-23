"""Unit tests for the ImmutableSandboxedEnvironment migration (PR #2408 / CWE-1336 hardening).

These tests assert two properties of the new Jinja2 sandbox:
  1. Safe template features (variable substitution, conditionals, loops, filters,
     dict-key access) render identically to the prior `Environment` behavior.
  2. Untrusted templates that would have escalated to RCE in the prior
     `Environment` are blocked with `jinja2.exceptions.SecurityError` under
     the sandboxed environment.

The intent is regression coverage: if a future refactor swaps the sandboxed
environment back to vanilla `Environment`, these tests fail loudly.
"""

from __future__ import annotations

import pytest
from jinja2 import StrictUndefined
from jinja2.exceptions import SecurityError
from jinja2.sandbox import ImmutableSandboxedEnvironment


@pytest.fixture
def env() -> ImmutableSandboxedEnvironment:
    """Mirror the construction used across pr_agent/tools/* and pr_agent/algo/."""
    return ImmutableSandboxedEnvironment(undefined=StrictUndefined)


class TestSandboxedSafeRendering:
    """Safe template features must continue to render identically."""

    def test_variable_substitution(self, env):
        out = env.from_string("{{ name }} reviewed {{ count }} PRs").render(
            name="pr-agent", count=42
        )
        assert out == "pr-agent reviewed 42 PRs"

    def test_conditional(self, env):
        tmpl = "{% if approved %}APPROVED{% else %}PENDING{% endif %}"
        assert env.from_string(tmpl).render(approved=True) == "APPROVED"
        assert env.from_string(tmpl).render(approved=False) == "PENDING"

    def test_for_loop(self, env):
        tmpl = "{% for f in files %}{{ f }};{% endfor %}"
        assert env.from_string(tmpl).render(files=["a.py", "b.py"]) == "a.py;b.py;"

    def test_dict_attribute_access(self, env):
        out = env.from_string("{{ pr.title }} by {{ pr.author }}").render(
            pr={"title": "Harden Jinja2", "author": "JAE0Y2N"}
        )
        assert out == "Harden Jinja2 by JAE0Y2N"

    def test_filter_pipeline(self, env):
        out = env.from_string("{{ items | length }}").render(items=[1, 2, 3])
        assert out == "3"


class TestSandboxedUnsafeBlocked:
    """Untrusted templates that would have escalated under vanilla Environment
    must raise SecurityError under the sandbox."""

    def test_dunder_class_access_blocked(self, env):
        # The classic SSTI primitive: reach into ``object`` via ``__class__``.
        # Under vanilla ``Environment`` this returns ``<class 'str'>``; under
        # the sandbox it MUST raise.
        with pytest.raises(SecurityError):
            env.from_string("{{ ''.__class__ }}").render()

    def test_mro_subclasses_blocked(self, env):
        # Reaching ``__subclasses__()`` would let an attacker enumerate every
        # Python type loaded in the process.
        with pytest.raises(SecurityError):
            env.from_string(
                "{{ ''.__class__.__mro__[1].__subclasses__() | length }}"
            ).render()

    def test_globals_os_chain_blocked(self, env):
        # Full RCE chain via ``cycler.__init__.__globals__.os.popen(...)``.
        # This is the payload that PROVES the prior unsandboxed environment
        # would have executed arbitrary shell.
        payload = "{{ cycler.__init__.__globals__['os'].popen('id').read() }}"
        with pytest.raises(SecurityError):
            env.from_string(payload).render()
