## Run as a Bitbucket Pipeline

You can use the Bitbucket Pipeline system to run PR-Agent on every pull request open or update.

1. Add the following file in your repository bitbucket-pipelines.yml

```yaml
pipelines:
    pull-requests:
      '**':
        - step:
            name: PR Agent Review
            image: pragent/pr-agent:latest
            script:
              - pr-agent --pr_url=https://bitbucket.org/$BITBUCKET_WORKSPACE/$BITBUCKET_REPO_SLUG/pull-requests/$BITBUCKET_PR_ID review
```

2. Add the following secure variables to your repository under Repository settings > Pipelines > Repository variables.

   - CONFIG__GIT_PROVIDER: `bitbucket`
   - OPENAI__KEY: `<your key>`
   - BITBUCKET__AUTH_TYPE: `basic` or `bearer` (default is `bearer`)
   - BITBUCKET__BEARER_TOKEN: `<your token>` (required when auth_type is bearer)
   - BITBUCKET__BASIC_TOKEN: `<your token>` (required when auth_type is basic)

You can get a Bitbucket token for your repository by following Repository Settings -> Security -> Access Tokens.
For basic auth, you can generate a base64 encoded token from your username:password combination.

Note that comments on a PR are not supported in Bitbucket Pipeline.

## Bitbucket Server and Data Center

Login into your on-prem instance of Bitbucket with your service account username and password.
Navigate to `Manage account`, `HTTP Access tokens`, `Create Token`.
Generate the token and add it to .secret.toml under `bitbucket_server` section

```toml
[bitbucket_server]
bearer_token = "<your key>"
```

Don't forget to also set the URL of your Bitbucket Server instance (either in `.secret.toml` or in `configuration.toml`):

```toml
[bitbucket_server]
url = "<full URL to your Bitbucket instance, e.g.: https://git.bitbucket.com>"
```

### Run it as CLI

Modify `configuration.toml`:

```toml
git_provider="bitbucket_server"
```



and pass the Pull request URL:

```shell
python cli.py --pr_url https://git.on-prem-instance-of-bitbucket.com/projects/PROJECT/repos/REPO/pull-requests/1 review
```

### Run it as service

To run PR-Agent as webhook, build the docker image:

```bash
docker build . -t pragent/pr-agent:bitbucket_server_webhook --target bitbucket_server_webhook -f docker/Dockerfile
docker push pragent/pr-agent:bitbucket_server_webhook  # Push to your Docker repository
```

Navigate to `Projects` or `Repositories`, `Settings`, `Webhooks`, `Create Webhook`.
Fill in the name and URL. For Authentication, select 'None'. Select the 'Pull Request Opened' checkbox to receive that event as a webhook.

The URL should end with `/webhook`, for example: https://domain.com/webhook
