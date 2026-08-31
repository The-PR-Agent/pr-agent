# Security Policy

PR-Agent is an open-source tool to help efficiently review and handle pull requests.

This document describes the security policy of the open-source PR-Agent project. It does not cover [Qodo](https://www.qodo.ai/), the separate commercial product that evolved out of the hosted Qodo Merge offering — for that, see Qodo's own security policy.

## PR-Agent Self-Hosted Solutions

When using PR-Agent with your OpenAI (or other LLM provider) API key, the security relationship is directly between you and the provider. PR-Agent does not send your code to any servers operated by the project.

Types of [self-hosted solutions](https://docs.pr-agent.ai/installation/):

- Locally
- GitHub integration
- GitLab integration
- BitBucket integration
- Azure DevOps integration

## PR-Agent Supported Versions

This section outlines which versions of PR-Agent are currently supported with security updates.

### Docker Deployment Options

#### Latest Version

For the most recent updates, use our latest Docker image which is automatically built nightly:

```yaml
uses: the-pr-agent/pr-agent@main
```

#### Specific Release Version

For a fixed version, you can pin your action to a specific release version. Browse available releases at:
[PR-Agent Releases](https://github.com/the-pr-agent/pr-agent/releases)

For example, to github action:

```yaml
steps:
  - name: PR Agent action step
    id: pragent
    uses: docker://pragent/pr-agent:0.41.0-github_action
```

Version tags are immutable — once published, `0.41.0-github_action` always resolves to the same image. Rolling tags such as `latest` and `github_action` are not; see the "Immutable releases and version tags" note on the [Installation page](https://docs.pr-agent.ai/installation/).

#### Enhanced Security with Docker Digest

For maximum security, you can specify the Docker image using its digest. Resolve the digest for the version you want to pin:

```sh
docker buildx imagetools inspect pragent/pr-agent:0.41.0-github_action --format '{{.Manifest.Digest}}'
```

Then reference it instead of the tag:

```yaml
steps:
  - name: PR Agent action step
    id: pragent
    uses: docker://pragent/pr-agent@sha256:<digest>
```

Official Docker Hub release images also publish GitHub Artifact Attestations, so you can verify a pinned digest before using it:

```sh
gh attestation verify \
  "oci://index.docker.io/pragent/pr-agent@sha256:<digest>" \
  --repo The-PR-Agent/pr-agent
```

## Reporting a Vulnerability

We take the security of PR-Agent seriously. If you discover a security vulnerability, please report it privately through GitHub's private vulnerability reporting, which is enabled on this repository:

[**Report a vulnerability**](https://github.com/The-PR-Agent/pr-agent/security/advisories/new)

Please include a description of the vulnerability, steps to reproduce, and the affected PR-Agent version.

Do not open a public issue for a security report — a public issue discloses the vulnerability before a fix is available.
