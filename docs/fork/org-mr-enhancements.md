# Org MR Enhancements (Fork)

This page documents the fork-owned toggles that gate the org-specific
GitLab MR `describe` enhancements. It is a reference for CI operators
and fork maintainers. It does not describe end-user MR authoring
workflow — that ships with the features in later phases.

## Toggles

Both toggles live in the `[pr_description]` section of
`pr_agent/settings/configuration.toml` and default to `false`. When
either is `false`, PR-Agent's default `describe` behavior is preserved
exactly — the fork adds no output.

| Key | Default | What it will gate |
| --- | --- | --- |
| `enable_conventional_title` | `false` | Rewriting the MR title to follow the Angular Commit Convention (`type(scope): summary`). Ships in Phase 2. |
| `enable_org_template` | `false` | Prepending the org's legacy MR description template (What / Note-Risk / Checklist) above PR-Agent's default output. Ships in Phase 3. |

Read pattern in code is `.get(key, False)`, so a downstream repo that
shadows the `[pr_description]` section in its own `.pr_agent.toml`
without repeating these keys still gets safe defaults.

## Enabling per CI invocation via environment variables

PR-Agent's configuration layer is built on
[dynaconf](https://www.dynaconf.com/). The fork does not add any new
plumbing for env-var overrides — it rides the existing env_loader
already wired up in `pr_agent/config_loader.py`. That loader uses:

- `envvar_prefix=False` — env var names carry no prefix like `DYNACONF_`
- `load_dotenv=False` — env vars must be set in the real process
  environment; a `.env` file will not be picked up
- The `SECTION__KEY` double-underscore convention to map an env var to
  a settings path

The two env vars are therefore:

| Env var | Effect |
| --- | --- |
| `PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true` | Sets `pr_description.enable_conventional_title` to `True` for the current process. |
| `PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true` | Sets `pr_description.enable_org_template` to `True` for the current process. |

Set them in the real environment before the PR-Agent CLI starts — for
example, in a GitLab CI job's `variables:` block, in a job-level
`before_script`, or on the invoking shell:

```bash
PR_DESCRIPTION__ENABLE_CONVENTIONAL_TITLE=true \
PR_DESCRIPTION__ENABLE_ORG_TEMPLATE=true \
    python -m pr_agent.cli --pr_url=... describe
```

Because `load_dotenv=False`, adding these to a `.env` file will not
work — they must be exported into the process environment by the CI
runner or the shell that invokes PR-Agent.

Coercion note: dynaconf coerces boolean-literal strings (`true`,
`True`, `false`, `False`) into real booleans. Prefer those literals
over integer values (`1`, `0`); integer literals are parsed as `int`,
which is still truthy but does not equal the boolean `True`. The
codebase reads these flags with `.get(key, False)`, so a truthy
value is enough to enable the feature, but sticking to boolean
literals keeps behavior predictable across settings backends.

## Verifying the override

The env override is exercised by
`tests/unittest/test_org_toggles_env_override.py`. That test builds a
fresh `Dynaconf` mirroring `config_loader.dynconf_kwargs` and asserts
that setting either env var flips its toggle to `True`. It exists
because dynaconf binds environment variables at `Dynaconf(...)`
construction time — the module-level singleton is built when
`pr_agent.config_loader` is first imported, so a late `os.environ`
change cannot affect it. Any test or diagnostic script that wants to
confirm the override path should follow the same fresh-Dynaconf
pattern.
