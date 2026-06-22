After [installation](../installation/index.md), there are three basic ways to invoke PR-Agent:

1. Locally running a CLI command
2. Online usage - by [commenting](https://github.com/the-pr-agent/pr-agent/pull/229#issuecomment-1695021901){:target="_blank"} on a PR
3. Enabling PR-Agent tools to run automatically when a new PR is opened

Specifically, CLI commands can be issued by invoking a pre-built [docker image](../installation/locally.md#using-docker-image), or by invoking a [locally cloned repo](../installation/locally.md#run-from-source).

For online usage, you will need to setup either a [GitHub App](../installation/github.md#run-as-a-github-app) or a [GitHub Action](../installation/github.md#run-as-a-github-action) (GitHub), a [GitLab webhook](../installation/gitlab.md#run-a-gitlab-webhook-server) (GitLab), or a [BitBucket App](../installation/bitbucket.md#run-using-codiumai-hosted-bitbucket-app) (BitBucket).
These platforms also enable to run PR-Agent specific tools automatically when a new PR is opened, or on each push to a branch.
