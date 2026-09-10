## Custom CA and self-signed certificates

When PR-Agent runs behind a corporate TLS-inspecting proxy or against servers using self-signed certificates, HTTPS calls to the LLM provider and git operations can fail with `certificate verify failed` errors. The fix requires two pieces: telling the HTTP clients which CA bundle to trust, and making LiteLLM use a transport that honours that bundle.

### Environment variables for CA trust

PR-Agent reads three environment variables when building the git clone environment. They are tried in the following order, and the first one that points to an existing file wins:

| Variable | Scope | Notes |
|---|---|---|
| `SSL_CERT_FILE` | git clone and Python `requests` | Preferred. Used by most Python SSL contexts. |
| `REQUESTS_CA_BUNDLE` | git clone and Python `requests` | Fallback. Honoured by the `requests` library directly. |
| `GIT_SSL_CAINFO` | git clone only | Last resort. Used by git when the other two are absent. |

If more than one variable is set and points to a different file, PR-Agent logs a warning and picks one according to the precedence above. All three variables should point to the same PEM file in practice.

Example (runner environment or shell profile):

```bash
export SSL_CERT_FILE=/etc/ssl/certs/corporate-ca-bundle.crt
```

Or in `.secrets.toml` (loaded once at startup):

```toml
[env]
SSL_CERT_FILE = "/etc/ssl/certs/corporate-ca-bundle.crt"
```

### LiteLLM transport fallback

By default LiteLLM uses aiohttp for HTTP calls. aiohttp does not honour the standard `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` variables. Setting `litellm.disable_aiohttp` makes LiteLLM fall back to the `requests`-based transport, which does.

```toml
[litellm]
disable_aiohttp = true
```

This setting is read once when the LiteLLM handler initialises, so it must be present before the process starts (runner environment or `.secrets.toml`, not a runtime override).

### Scope of `gitlab.ssl_verify`

The `gitlab.ssl_verify` setting (or `gitlab__SSL_VERIFY` environment variable) is passed only to the python-gitlab client that talks to the GitLab API. It does **not** affect the LLM call, git clone operations, or any other HTTPS client in the process. If you are seeing certificate errors from LiteLLM or git clone, use the environment variables above instead.

### Putting it all together

For a runner behind a corporate proxy with a custom CA:

1. Export `SSL_CERT_FILE` (or `REQUESTS_CA_BUNDLE` / `GIT_SSL_CAINFO`) pointing to your CA bundle.
2. Set `litellm.disable_aiohttp = true` in your configuration.
3. If you also use the GitLab API, keep `gitlab.ssl_verify` pointed at the same bundle for the python-gitlab client.
