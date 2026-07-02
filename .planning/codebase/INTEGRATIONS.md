# External Integrations

**Analysis Date:** 2026-07-02

## APIs & External Services

**LLM Providers (via LiteLLM gateway `pr_agent/algo/ai_handlers/litellm_ai_handler.py`):**
- OpenAI - Primary LLM provider (GPT-5.5, GPT-5.4-mini, GPT-5.4-nano)
  - SDK/Client: `openai >=1.55.3`, `litellm 1.84.0`
  - Auth: `OPENAI.KEY` setting or `OPENAI_API_KEY` env var
- Anthropic/Claude - Alternative LLM provider
  - SDK/Client: `anthropic >=0.69.0`
  - Auth: `ANTHROPIC.KEY` setting
  - Features: Extended thinking support (`enable_claude_extended_thinking` config)
- Google Vertex AI - Alternative LLM provider
  - SDK/Client: `google-cloud-aiplatform 1.154.0`
  - Auth: Google Cloud credentials
- AWS Bedrock - Alternative LLM provider
  - SDK/Client: `boto3 1.40.45` (via LiteLLM)
  - Auth: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or IMDS/IRSA (`AWS_USE_IMDS=true`)

**Git Platform APIs:**
- GitHub - Primary git platform integration
  - SDK/Client: `PyGithub 1.59.*` (`pr_agent/git_providers/github_provider.py`)
  - Auth: GitHub App (JWT via `GITHUB_APP.PRIVATE_KEY`) or Personal Access Token (`GITHUB.USER_TOKEN`)
  - Endpoints: PRs, comments, reviews, labels, checks
- GitLab - Git platform integration
  - SDK/Client: `python-gitlab 8.3.0` (`pr_agent/git_providers/gitlab_provider.py`)
  - Auth: `GITLAB.PERSONAL_ACCESS_TOKEN` or webhook secret
  - Config: `GITLAB.URL` (defaults to `https://gitlab.com`)
- Azure DevOps - Git platform integration
  - SDK/Client: `azure-devops 7.1.0b4` (`pr_agent/git_providers/azuredevops_provider.py`)
  - Auth: `azure-identity 1.25.0`, `msrest 0.7.1`
- Bitbucket (Cloud & Server) - Git platform integration
  - SDK/Client: `atlassian-python-api 3.41.4` (`pr_agent/git_providers/bitbucket_provider.py`, `bitbucket_server_provider.py`)
  - Auth: Atlassian App credentials or webhook tokens
- Gitea - Git platform integration
  - SDK/Client: `giteapy 1.0.8` (`pr_agent/git_providers/gitea_provider.py`)
  - Auth: Access token
  - Config: `GITEA.URL` (defaults to `https://gitea.com`)
- Gerrit - Git platform integration
  - SDK/Client: Custom HTTP client (`pr_agent/git_providers/gerrit_provider.py`)
- AWS CodeCommit - Git platform integration
  - SDK/Client: `boto3` (`pr_agent/git_providers/codecommit_provider.py`, `codecommit_client.py`)
  - Auth: AWS credentials

## Data Storage

**Databases:**
- None required by default (stateless design)
- Optional: Pinecone, LanceDB, Qdrant (for `/similar_issue` tool, commented out in `requirements.txt`)

**File Storage:**
- Google Cloud Storage - Secret storage and configuration
  - SDK/Client: `google-cloud-storage 2.10.0` (`pr_agent/secret_providers/google_cloud_storage_secret_provider.py`)
  - Auth: GCS service account JSON (`GOOGLE_CLOUD_STORAGE.SERVICE_ACCOUNT`)
  - Config: `GOOGLE_CLOUD_STORAGE.BUCKET_NAME`

**Caching:**
- None (stateless request processing)

## Authentication & Identity

**GitHub App Authentication:**
- Implementation: JWT-based (`PyJWT 2.10.1`)
- Flow: Private key signs JWT → exchange for installation token
- Config: `GITHUB_APP.APP_ID`, `GITHUB_APP.PRIVATE_KEY`
- Provider: `pr_agent/git_providers/github_provider.py`

**Identity Provider Interface:**
- Abstraction: `pr_agent/identity_providers/identity_provider.py`
- Default: `pr_agent/identity_providers/default_identity_provider.py`
- Purpose: Eligibility checks for users/organizations

**Webhook Signature Verification:**
- GitHub: HMAC-SHA256 signature verification (`pr_agent/servers/utils.py`)
- GitLab: Secret token verification
- Bitbucket: Atlassian Connect JWT

## Monitoring & Observability

**LLM Observability:**
- Langfuse 3.14.5 - LLM call tracing and monitoring (`pr_agent/mosaico/observability.py`)

**Logs:**
- Loguru 0.7.2 (`pr_agent/log/__init__.py`)
- Formats: JSON (production), Console (development)
- Structured logging with request context

**Code Coverage:**
- Codecov (`codecov.yml` in repo root)

## CI/CD & Deployment

**Hosting Options:**
- Docker containers (multi-stage `docker/Dockerfile`)
  - Targets: `github_app`, `bitbucket_app`, `bitbucket_server_webhook`, `github_polling`, `gitlab_webhook`, `azure_devops_webhook`, `gitea_app`, `mosaico_agent`
- AWS Lambda (`docker/Dockerfile.lambda`, `pr_agent/servers/github_lambda_webhook.py`, `pr_agent/servers/gitlab_lambda_webhook.py`)
  - Adapter: Mangum (ASGI-to-Lambda bridge)
- GitHub Actions (`Dockerfile.github_action`, `action.yaml`)

**CI Pipeline:**
- GitHub Actions (implied by `action.yaml`, `github_action/` directory)
- Codecov for coverage reporting (`codecov.yml`)

## Environment Configuration

**Required env vars (minimum):**
- LLM API key (one of): `OPENAI_API_KEY` / `ANTHROPIC.KEY` / AWS credentials
- Git provider credentials (one of): GitHub App keys / GitLab token / Azure DevOps token / Bitbucket credentials

**Optional env vars:**
- `CONFIG.SECRET_PROVIDER` - Enable secret management ("google_cloud_storage" or "aws_secrets_manager")
- `AWS_USE_IMDS` - Use ambient AWS credentials from IMDS/task-role/IRSA
- `CONFIG.LOG_LEVEL` - Log verbosity (default: DEBUG)

**Secrets Management:**
- AWS Secrets Manager (`pr_agent/secret_providers/aws_secrets_manager_provider.py`)
  - Config: `AWS_SECRETS_MANAGER.SECRET_ARN`, `AWS_SECRETS_MANAGER.REGION_NAME`
- Google Cloud Storage (`pr_agent/secret_providers/google_cloud_storage_secret_provider.py`)
  - Config: `GOOGLE_CLOUD_STORAGE.SERVICE_ACCOUNT`, `GOOGLE_CLOUD_STORAGE.BUCKET_NAME`
- Local `.secrets.toml` files (`pr_agent/settings/.secrets.toml`, `settings_prod/.secrets.toml`)

## Webhooks & Callbacks

**Incoming Webhooks:**
- GitHub: `POST /api/v1/github_webhooks` (`pr_agent/servers/github_app.py`)
- GitHub Marketplace: `POST /api/v1/marketplace_webhooks` (`pr_agent/servers/github_app.py`)
- GitLab: Webhook endpoint (`pr_agent/servers/gitlab_webhook.py`)
- Bitbucket Cloud: `pr_agent/servers/bitbucket_app.py`
- Bitbucket Server: `pr_agent/servers/bitbucket_server_webhook.py`
- Azure DevOps: `pr_agent/servers/azuredevops_server_webhook.py`
- Gerrit: `pr_agent/servers/gerrit_server.py`
- Gitea: `pr_agent/servers/gitea_app.py`

**Outgoing:**
- PR comments/reviews posted to git platforms via their respective APIs
- LLM API calls to configured provider endpoints

## Agent-to-Agent Protocol

**A2A SDK:**
- Package: `a2a-sdk[http-server] 1.0.3`
- Implementation: `pr_agent/mosaico/` directory
- Purpose: Multi-agent orchestration (dispatch, execution, card-based responses)
- Files: `pr_agent/mosaico/server.py`, `pr_agent/mosaico/executor.py`, `pr_agent/mosaico/dispatch.py`

---

*Integration audit: 2026-07-02*
