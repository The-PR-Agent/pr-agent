# Installation

There are several ways to use PR-Agent:

- [Locally](./locally.md)
- [GitHub integration](./github.md)
- [GitLab integration](./gitlab.md)
- [BitBucket integration](./bitbucket.md)
- [Azure DevOps integration](./azure.md)
- [Gitea integration](./gitea.md)

!!! note "Docker Hub namespace migration"
    Releases **`0.34.2` and later** are published under [`pragent/pr-agent`](https://hub.docker.com/r/pragent/pr-agent). Older releases (up to and including `v0.31`) remain at the legacy [`codiumai/pr-agent`](https://hub.docker.com/r/codiumai/pr-agent) namespace as a frozen archive — no new images are pushed there. The examples on this site reference the new namespace; if you are pinning to a release before `0.34.2`, swap `pragent/pr-agent` for `codiumai/pr-agent` in your `image:` / `docker pull` / `uses: docker://` references.
