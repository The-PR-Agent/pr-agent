# Swapping the reviewer image to a fork-built image

This page is for operators maintaining a **self-hosted pr-agent reviewer** (the
`github_app` server). It documents how to replace the running reviewer image with
one **built from this fork's audited source** rather than the unverified
`pragent/pr-agent` Docker Hub base.

## Why

The running `pr-agent-reviewer` container was built `FROM pragent/pr-agent:0.39.0-github_app`,
a Docker Hub image whose official provenance is unconfirmed. Because that container
reads every line of private source and holds a GitHub App credential, we build the
replacement image **from this fork's source** so the running image matches code we
control.

The replacement is a **drop-in**: same `WORKDIR`, same `CMD` (gunicorn serving
`pr_agent.servers.github_app:app`), same exposed port `3000/tcp`, same `ENTRYPOINT`
(none), and the GitHub App key/webhook secret/LiteLLM `api_base` are **not** baked
into the image — they are bind-mounted at runtime from `~/.pr-agent/.secrets.toml`,
exactly as today.

> **Safety:** building a new image never touches the running `pr-agent-reviewer`
> container. Only the final swap step stops/restarts it.

## What was built

- **Dockerfile:** the repository root `Dockerfile` (single-purpose `github_app` image).
- **Base image:** the **official** `python:3.12.13-slim`, pinned by sha256 digest:
  ```
  FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
  ```
  The digest was captured on 2025-08-04 from `docker.io/library/python` via
  `docker pull python:3.12.13-slim` then
  `docker inspect --format '{{.RepoDigests}}' python:3.12.13-slim`.
- **No secrets in the image:** `.dockerignore` excludes `pr_agent/settings/.secrets.toml`
  and `**/.env`; `config_loader` reads them from the runtime bind-mount instead.

### Verification performed

- `docker build` succeeded → image `pr-agent-reviewer:0.41.0-fork` (id `5664832a49d4`).
- Config matches the live container: `WORKDIR=/app`, `CMD` identical, `ENTRYPOINT`
  empty, `EXPOSE 3000`, `ENV PYTHONPATH=/app`.
- A **throwaway** container (`pr-agent-reviewer-test-fork`) was started on host port
  `3999` (container `3000`), confirmed `curl http://127.0.0.1:3999/` →
  `{"status":"ok"}` (HTTP 200), then stopped and **removed**. The live
  `pr-agent-reviewer` container was never touched.

## Prerequisites

Run these on the captain's Mac (colima docker). The docker CLI lives at
`/opt/homebrew/bin/docker`:

```bash
export PATH="/opt/homebrew/bin:$PATH"
# colima must be running
colima status
```

## 1. Build the image

From a checkout of **this fork** (the worktree), at the repository root:

```bash
docker build -t pr-agent-reviewer:0.41.0-fork .
```

The build context is the repo root; the root-level `Dockerfile` is used by default.

## 2. Verify on a throwaway port

Start the new image on an unused host port (pick one that is free, e.g. `3999`)
without the secrets mount — the health endpoint does not need credentials:

```bash
docker run -d --name pr-agent-reviewer-verify -p 3999:3000 pr-agent-reviewer:0.41.0-fork
sleep 20   # gunicorn + litellm import takes ~15s
curl -sS -i http://127.0.0.1:3999/   # expect: HTTP/1.1 200 OK  {"status": "ok"}
docker rm -f pr-agent-reviewer-verify
```

## 3. Push (optional)

If you want the image in a registry (for backup or multi-host use), tag and push.
There is no registry on this Mac by default, so this step is **optional**; the
redeploy below uses the local image.

```bash
docker tag pr-agent-reviewer:0.41.0-fork <your-registry>/pr-agent-reviewer:0.41.0-fork
docker push <your-registry>/pr-agent-reviewer:0.41.0-fork
# (on the host) use <your-registry>/pr-agent-reviewer:0.41.0-fork as the image in step 4
```

## 4. Swap the running reviewer

> The current container is named `pr-agent-reviewer`, restarts `unless-stopped`,
> binds `127.0.0.1:3033->3000`, and bind-mounts your secrets. Reproduce those
> exactly; only the image changes.

```bash
export PATH="/opt/homebrew/bin:$PATH"

# Stop and remove the OLD reviewer (the poller is NOT affected by this image swap).
docker stop pr-agent-reviewer
docker rm   pr-agent-reviewer

# Start the NEW fork-built image with the SAME name, restart policy, port, and mounts.
docker run -d \
  --name pr-agent-reviewer \
  --restart unless-stopped \
  -p 127.0.0.1:3033:3000 \
  -v /Users/dep/.pr-agent/.secrets.toml:/app/pr_agent/settings/.secrets.toml \
  -v /Users/dep/.pr-agent/pr_reviewer_prompts.toml:/app/pr_agent/settings/pr_reviewer_prompts.toml \
  pr-agent-reviewer:0.41.0-fork

# Confirm it came up and answers the health path.
sleep 20
curl -sS -i http://127.0.0.1:3033/
docker ps --filter name=pr-agent-reviewer
```

After the swap, watch the logs for a few review events:

```bash
docker logs -f pr-agent-reviewer
```

## Roll back

If the new image misbehaves, revert to the prior image in place — same steps,
old image:

```bash
export PATH="/opt/homebrew/bin:$PATH"

docker stop pr-agent-reviewer
docker rm   pr-agent-reviewer

docker run -d \
  --name pr-agent-reviewer \
  --restart unless-stopped \
  -p 127.0.0.1:3033:3000 \
  -v /Users/dep/.pr-agent/.secrets.toml:/app/pr_agent/settings/.secrets.toml \
  -v /Users/dep/.pr-agent/pr_reviewer_prompts.toml:/app/pr_agent/settings/pr_reviewer_prompts.toml \
  pr-agent-reviewer:sparky

sleep 20
curl -sS -i http://127.0.0.1:3033/
```

The old image (`pr-agent-reviewer:sparky`) is untouched on disk, so rollback is
instant — only the running container's image reference changes.

## Notes

- Never build secrets into the image. Keep `pr_agent/settings/.secrets.toml` and
  any `.env` out of the build context (the root `.dockerignore` already excludes
  them); supply them only via the runtime bind-mounts above.
- The `pr-agent-poller` companion container is independent of this image swap and
  is not stopped or restarted by these steps.
- The GitHub App held by the reviewer is the one configured in
  `~/.pr-agent/.secrets.toml`; this swap does not change the App, its scopes, or
  its installation.
