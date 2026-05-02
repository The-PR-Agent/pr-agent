# Security Policy

PR-Agent is an open-source tool to help efficiently review and handle pull requests. Qodo Merge is a paid version of PR-Agent, designed for companies and teams that require additional features and capabilities.

This document describes the security policy of PR-Agent. For Qodo Merge's security policy, see [here](https://qodo-merge-docs.qodo.ai/overview/data_privacy/#qodo-merge).

## PR-Agent Self-Hosted Solutions

When using PR-Agent with your OpenAI (or other LLM provider) API key, the security relationship is directly between you and the provider. We do not send your code to Qodo servers.

Types of [self-hosted solutions](https://qodo-merge-docs.qodo.ai/installation):

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
uses: qodo-ai/pr-agent@main
```

#### Specific Release Version

For a fixed version, you can pin your action to a specific release version. Browse available releases at:
[PR-Agent Releases](https://github.com/qodo-ai/pr-agent/releases)

For example, to github action:

```yaml
steps:
  - name: PR Agent action step
    id: pragent
    uses: docker://pragent/pr-agent:0.34.2-github_action
```

#### Enhanced Security with Docker Digest

For maximum security, you can specify the Docker image using its digest:

```yaml
steps:
  - name: PR Agent action step
    id: pragent
    uses: docker://pragent/pr-agent@sha256:a0b36966ca3a197ca739fa1e65c16703076fc1c744cd423ca203b8c21707d71c
```

## Reporting a Vulnerability

We take the security of PR-Agent seriously. If you discover a security vulnerability, please report it immediately to:

Email: security@qodo.ai

Please include a description of the vulnerability, steps to reproduce, and the affected PR-Agent version.
