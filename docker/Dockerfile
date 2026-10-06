FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /uvx /bin/

# litellm fetches its model cost map over the network at import time, so a
# third-party change to that map can change runtime behavior with no image
# change and no version bump. Use the map bundled with the litellm pinned in
# uv.lock so every stage (test and runtime) behaves how it was built and tested
# rather than tracking today's remote data; new models then arrive with a
# deliberate litellm bump in the lockfile.
ENV LITELLM_LOCAL_MODEL_COST_MAP=True

RUN apt-get update && apt-get install --no-install-recommends -y git curl && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
COPY docs docs
RUN uv sync --frozen --no-install-project --no-dev --extra all
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

# Use a dedicated identity for long-running services; preserve the runner, CLI
# and test mount/install contracts. Grant UID/GID 10001 read access to mounted
# secrets and write access to custom output, analytics and database paths.
FROM base AS service
RUN groupadd --gid 10001 pragent && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin pragent && \
    mkdir -p /app/pr-agent-outputs /app/lancedb && \
    chown pragent:pragent /app/pr-agent-outputs /app/lancedb
ENV HOME=/home/pragent
USER 10001:10001

FROM service AS github_app
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]

FROM service AS bitbucket_app
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/bitbucket_app.py"]

FROM service AS bitbucket_server_webhook
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.bitbucket_server_webhook:app"]

FROM service AS github_polling
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/github_polling.py"]

FROM base AS github_action
COPY pr_agent pr_agent
COPY github_action/entrypoint.sh /
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

FROM service AS gitlab_webhook
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.gitlab_webhook:app"]

FROM service AS azure_devops_webhook
COPY pr_agent pr_agent
CMD ["python", "pr_agent/servers/azuredevops_server_webhook.py"]

FROM service AS gitea_app
COPY pr_agent pr_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py","pr_agent.servers.gitea_app:app"]

FROM service AS mosaico_agent
COPY pr_agent pr_agent
EXPOSE 9000
CMD ["python", "pr_agent/mosaico/server.py"]


FROM base AS test
# The base stage pins LITELLM_LOCAL_MODEL_COST_MAP for every stage; kept here
# only as documentation that the unit suite keys off the bundled cost map.
RUN uv sync --frozen --no-install-project --extra all
COPY pr_agent pr_agent
COPY tests tests
# Config the unit suite asserts against, copied file by file so an unrelated
# workflow edit does not invalidate this layer.
COPY .github/workflows/publish.yml .github/workflows/publish.yml
COPY .github/workflows/build-and-test.yaml .github/workflows/build-and-test.yaml
COPY docker/Dockerfile docker/Dockerfile
COPY docker/Dockerfile.lambda docker/Dockerfile.lambda
COPY docker/mosaico docker/mosaico
# Read by the docs guards: the URL checker, the pages that link into the docs site,
# and the packaging inputs that must ship every /help page suffix. Kept last so a
# README edit only rebuilds these final layers.
COPY scripts/check_docs_urls.py scripts/check_docs_urls.py
COPY README.md CONTRIBUTING.md SECURITY.md .dockerignore ./
COPY MANIFEST.in setup.py ./

FROM base AS cli
COPY pr_agent pr_agent
RUN uv sync --frozen --no-dev --extra all && ln -s /app/.venv/bin/pr-agent /usr/local/bin/pr-agent
ENTRYPOINT ["python", "pr_agent/cli.py"]
