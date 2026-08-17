# Atlara deployment notes

This fork of [PR-Agent](https://github.com/The-PR-Agent/pr-agent) is Atlara's free AI
code review GitHub App. It is a thin layer on top of upstream PR-Agent: the diff
parsing, GitHub App auth, and suggestion-posting logic are untouched — the only
integration point is the model backend, which is pointed at Atlara's router
instead of a direct OpenAI/Anthropic key.

## How the backend is wired

PR-Agent's `LiteLLMAIHandler` already has a first-class path for OpenAI-compatible
custom endpoints (the same path used for self-hosted vLLM/LM Studio deployments):
setting `[openai].api_base` routes every chat-completion call through that base URL
instead of `api.openai.com`, using the model id in `[config].model`.

`pr_agent/settings/atlara.toml` sets:
- `openai.api_base = "https://cloud.atlara.ai/v1"` — Atlara's router
- `config.model = "openai/<catalogue-model-id>"` — a model from Atlara's live
  `model_catalogue`, not a guess. **This still needs to be filled in** with a
  confirmed coding-capable, cost-appropriate catalogue id before this ships.

The API key is deliberately **not** in that file. Dynaconf's env loader overlays
env vars onto the nested toml keys as `SECTION__SETTING`, so the deploy environment
sets:

```
OPENAI__KEY=<atlara-issued API key for the atlara-review service account>
```

That key should belong to a dedicated Atlara org/service account so free-tier
review spend is trackable and capped separately from any other traffic, per the
credit-metering plan (see the growth-strategy memo this fork implements).

## Still to build (not in this change)

This commit only wires the model backend. Still open, per the plan:

1. **Credit-aware routing.** Free reviews should debit a bounded per-org credit
   grant through the same `recordUsage`/`debitCredits` path the router already
   uses for other inference, not call the router unmetered. This needs either a
   dedicated Atlara API key per installing org (simplest, reuses existing
   per-org billing) or a new PR-Agent-side usage hook — needs a decision before
   this goes further than a single shared key.
2. **CTA injection.** The three passive footer variants and the "out of
   credits" fork-in-the-road comment are not implemented. They belong in the
   comment-formatting layer (`pr_agent/tools/pr_reviewer.py` and
   `pr_agent/tools/pr_code_suggestions.py` are the likely insertion points —
   not yet investigated in depth).
3. **GitHub App registration + webhook deployment.** `github.deployment_type =
   "app"` plus `app_id`/`private_key`/`webhook_secret` in secrets are already
   supported by upstream PR-Agent; a real Atlara GitHub App still needs to be
   registered and those secrets provisioned.
4. **Where this runs.** Per the plan, this should be its own small stateless
   service, not folded into `neuralgrid-router-go` or `neuralgrid-web`. Hosting
   (same prod VM vs. separate) not yet decided.
