FROM python:3.12.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

RUN apt-get update && apt-get install --no-install-recommends -y git curl && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
COPY docs docs
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

FROM base AS github_app
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]

FROM base AS bitbucket_app
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_app.py"]

FROM base AS bitbucket_server_webhook
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_server_webhook.py"]

FROM base AS github_polling
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/github_polling.py"]

FROM base AS github_action
COPY pr_agent pr_agent
COPY github_action/entrypoint.sh /
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

FROM base AS gitlab_webhook
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.gitlab_webhook:app"]

FROM base AS azure_devops_webhook
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/azuredevops_server_webhook.py"]

FROM base AS gitea_app
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py","pr_agent.servers.gitea_app:app"]

FROM base AS mosaico_agent
COPY pr_agent pr_agent
EXPOSE 9000
CMD ["python", "pr_agent/mosaico/server.py"]


FROM base AS test
RUN uv sync --frozen --no-install-project
COPY pr_agent pr_agent
COPY tests tests
COPY .github/workflows/publish.yml .github/workflows/publish.yml
COPY docker/mosaico docker/mosaico

FROM base AS cli
COPY pr_agent pr_agent
RUN uv sync --frozen --no-dev && ln -s /app/.venv/bin/pr-agent /usr/local/bin/pr-agent
ENTRYPOINT ["python", "pr_agent/cli.py"]
