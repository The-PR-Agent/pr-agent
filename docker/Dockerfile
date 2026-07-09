FROM python:3.12.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

RUN apt update && apt install --no-install-recommends -y git curl && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ADD pyproject.toml uv.lock ./
ADD docs docs
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

FROM base AS github_app
ADD pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]

FROM base AS bitbucket_app
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_app.py"]

FROM base AS bitbucket_server_webhook
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_server_webhook.py"]

FROM base AS github_polling
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/github_polling.py"]

FROM base AS gitlab_webhook
ADD pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.gitlab_webhook:app"]

FROM base AS azure_devops_webhook
ADD pr_agent pr_agent
CMD ["python", "pr_agent/servers/azuredevops_server_webhook.py"]

FROM base AS gitea_app
ADD pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py","pr_agent.servers.gitea_app:app"]

FROM base AS mosaico_agent
ADD pr_agent pr_agent
EXPOSE 9000
CMD ["python", "pr_agent/mosaico/server.py"]


FROM base AS test
RUN uv sync --frozen --no-install-project
ADD pr_agent pr_agent
ADD tests tests
ADD docker/mosaico docker/mosaico

FROM base AS cli
ADD pr_agent pr_agent
ENTRYPOINT ["python", "pr_agent/cli.py"]
