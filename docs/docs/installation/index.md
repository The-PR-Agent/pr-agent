# Installation

There are several ways to use PR-Agent:

- [Locally](./locally.md)
- [GitHub integration](./github.md)
- [GitLab integration](./gitlab.md)
- [BitBucket integration](./bitbucket.md)
- [Azure DevOps integration](./azure.md)
- [Gitea integration](./gitea.md)

## Sizing a self-hosted webhook server

The GitHub, GitLab and Gitea webhook servers (the `github_app`, `gitlab_webhook` and `gitea_app` Docker targets) run under gunicorn with multiple worker processes, so that a worker busy handling a request cannot block the health check served by another. The other deployments — Bitbucket, Azure DevOps, GitHub polling, and the Lambda variants — do not use this gunicorn worker configuration.

| Variable | Default | Description |
|----------|---------|-------------|
| `GUNICORN_WORKERS` | *(unset)* | Pins the worker count exactly. Overrides everything below. |
| `GUNICORN_MAX_WORKERS` | `4` | Upper bound on the automatically derived worker count. |

When `GUNICORN_WORKERS` is unset, the worker count is derived from the CPUs actually available to the container (cgroup CPU limit, falling back to CPU affinity), clamped to between 2 and `GUNICORN_MAX_WORKERS`. Note that a CPU *request* without a *limit* leaves no cgroup quota to read, so the cap is what bounds the worker count there.

**Sizing memory:** importing the application costs roughly 250MB. The app is imported once in the gunicorn master and workers are forked from it, so they share most of that baseline copy-on-write rather than each paying it in full — total memory grows with worker count, but by noticeably less than 250MB per worker. Start from about 1Gi for the default of 4 workers and adjust against your own metrics.

If the container is OOMKilled during startup, lower the worker count:

```bash
GUNICORN_WORKERS=2
```

## GitHub polling workers

GitHub polling uses one polling process and a separate child process for each
accepted comment. At most 10 of these child workers may be active across all
polling iterations. If an earlier batch still fills the limit, accepted work
waits for a free slot without blocking the event loop. This can delay the next
notification poll under sustained load.

The existing per-batch limit is unchanged: only the first 10 queued comments in
a batch are accepted; excess work is logged and dropped. Waiting work is held in
memory only. Notification acknowledgement is unchanged, so shutdown or startup
failures do not guarantee redelivery. Dispatch interruptions log the remaining
count. A worker startup failure stops polling rather than risking further
dispatch with an untracked child; inspect the failure before restarting.
Completed children are joined without waiting and closed each iteration;
live children are not terminated on cancellation. This is not a durable queue,
a model-call deadline, or a host-wide memory limit.

!!! note "Docker Hub namespace migration"
    Releases **`0.34.2` and later** are published under [`pragent/pr-agent`](https://hub.docker.com/r/pragent/pr-agent). Older releases (up to and including `v0.31`) remain at the legacy [`codiumai/pr-agent`](https://hub.docker.com/r/codiumai/pr-agent) namespace as a frozen archive — no new images are pushed there. The examples on this site reference the new namespace; if you are pinning to a release before `0.34.2`, swap `pragent/pr-agent` for `codiumai/pr-agent` in your `image:` / `docker pull` / `uses: docker://` references.

!!! note "Immutable releases and version tags"
    **What you pin is what you get.** Version-numbered artifacts can never change after publication:

    - **GitHub releases** — the Git tag cannot be moved or deleted, and attached assets cannot be added, replaced, or removed. The protection also survives repository deletion, so a tag from an immutable release can never be reused by a repository recreated under the same name. (Release titles and notes stay editable; immutability covers the tag and the assets.)
    - **Docker images** — version tags such as `0.40.0` and `0.40.0-github_app` always resolve to the same image. Once pushed, they cannot be overwritten or repointed.

    **Rolling tags stay mutable by design.** `latest`, `github_action`, `github_lambda`, `gitlab_lambda`, `gitlab_webhook`, `gitea_app`, `mosaico_agent` and `bitbucket_server_webhook` move to the newest build on every release. They are convenient for trying things out, but a `docker pull` of the same rolling tag on two different days can give you two different images.

    For anything you depend on — CI, production webhooks, pinned Action steps — reference a version tag (or a digest) rather than a rolling one. Upgrading then becomes a deliberate change you make, not something that happens underneath you.
