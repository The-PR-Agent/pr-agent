# =============================================================================
# pr-agent reviewer image, built FROM THIS FORK'S SOURCE.
#
# Drop-in replacement for the previously-running `pragent/pr-agent:0.39.0-github_app`
# image. It intentionally mirrors that image's contract so the captain can swap it
# in without changing ports, env, mounts, or the running command:
#
#   WORKDIR             /app
#   ENTRYPOINT          (none)
#   CMD                 python -m gunicorn -k uvicorn.workers.UvicornWorker \
#                           -c pr_agent/servers/gunicorn_config.py \
#                           --forwarded-allow-ips * \
#                           pr_agent.servers.github_app:app
#   EXPOSED PORT        3000/tcp   (gunicorn binds 0.0.0.0:3000 by default)
#   ENV                 PYTHONPATH=/app   (base image vars are inherited)
#
# NO secrets are baked into this image. The GitHub App private key, app id,
# webhook secret and LiteLLM api_base are read at runtime from
#   /app/pr_agent/settings/.secrets.toml   (mounted from ~/.pr-agent/.secrets.toml)
# exactly as the current container expects; `.dockerignore` excludes
# `pr_agent/settings/.secrets.toml` and `**/.env` from the build context.
#
# -----------------------------------------------------------------------------
# Base image: the OFFICIAL python:3.12.13-slim from Docker Hub, pinned by its
# sha256 digest (not a floating tag) so the build is reproducible and tamper-evident.
#
# Digest captured on 2025-08-04 from docker.io/library/python (Docker Hub) via:
#     docker pull python:3.12.13-slim
#     docker inspect --format '{{.RepoDigests}}' python:3.12.13-slim
# => python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
# ---------------------------------------------------------------------------
FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

RUN apt-get update \
    && apt-get install --no-install-recommends -y git curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package once (dependencies are pulled from requirements.txt via
# pyproject.toml) so this layer is cached independently of the app source.
ADD pyproject.toml .
ADD requirements.txt .
ADD docs docs
RUN pip install --no-cache-dir . && rm pyproject.toml requirements.txt

ENV PYTHONPATH=/app

FROM base AS github_app

# Application source only. No .secrets.toml / .env is copied here (see
# `.dockerignore`); config_loader reads secrets from the runtime bind-mount.
ADD pr_agent pr_agent

# Same port the upstream image exposes and the captain's run config maps.
EXPOSE 3000

# Same CMD as pragent/pr-agent:0.39.0-github_app. No ENTRYPOINT is set, so
# `docker run` execs gunicorn directly. Per-container runtime values (GitHub App
# key, api_base, PORT, etc.) come from the existing ~/.pr-agent/.secrets.toml
# bind-mount, not from the image.
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]
