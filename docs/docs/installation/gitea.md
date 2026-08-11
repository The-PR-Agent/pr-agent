## Run a Gitea webhook server

1. In Gitea create a new user and give it "Reporter" role for the intended group or project.

2. For the user from step 1. generate a `personal_access_token` with `api` access.

3. Generate a random secret for your app, and save it for later (`webhook_secret`). For example, you can use:

    ```bash
    WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(10))")
    ```

4. Clone this repository:

    ```bash
    git clone https://github.com/the-pr-agent/pr-agent.git
    ```

5. Prepare variables and secrets. Skip this step if you plan on setting these as environment variables when running the agent:
    - In the configuration file/variables:
        - Set `config.git_provider` to "gitea"
    - In the secrets file/variables:
        - Set your AI model key in the respective section
        - In the [Gitea] section, set `personal_access_token` (with token from step 2) and `webhook_secret` (with secret from step 3)

6. Build a Docker image for the app and optionally push it to a Docker repository. We'll use Dockerhub as an example:

    ```bash
    docker build . -t pr-agent:gitea_app --target gitea_app -f docker/Dockerfile

    # Optional, to push it to your own Docker repository:
    docker tag pr-agent:gitea_app <your-registry>/pr-agent:gitea_app
    docker push <your-registry>/pr-agent:gitea_app
    ```

7. Set the environmental variables, the method depends on your docker runtime. Skip this step if you included your secrets/configuration directly in the Docker image.

    ```bash
    CONFIG__GIT_PROVIDER=gitea
    GITEA__PERSONAL_ACCESS_TOKEN=<personal_access_token>
    GITEA__WEBHOOK_SECRET=<webhook_secret>
    GITEA__URL=https://gitea.com # Or self host
    GITEA__WEB_URL=https://git.example.com # Optional: user-facing URL for links published in comments (see below)
    OPENAI__KEY=<your_openai_api_key>
    GITEA__SKIP_SSL_VERIFICATION=false # or true
    GITEA__SSL_CA_CERT=/path/to/cacert.pem
    ```

    Links published in comments are built from `GITEA__WEB_URL` when set, else from `GITEA__URL`
    when it differs from the shipped default (`https://gitea.com`), else derived from the PR's
    `html_url` (which Gitea/Forgejo builds from its own `ROOT_URL`).
    Set `GITEA__WEB_URL` explicitly when `GITEA__URL` is an internal address users cannot browse
    (e.g. a Docker service name), or when the server's `ROOT_URL` is misconfigured.

8. Create a webhook in your Gitea project. Set the URL to `http[s]://<PR_AGENT_HOSTNAME>/api/v1/gitea_webhooks`, the secret token to the generated secret from step 3, and enable the triggers `push`, `comments` and `merge request events`.

9. Test your installation by opening a merge request or commenting on a merge request using one of PR Agent's commands.

10. The webhook server runs under gunicorn with multiple worker processes. See [Sizing a self-hosted webhook server](./index.md#sizing-a-self-hosted-webhook-server) for the `GUNICORN_WORKERS` / `GUNICORN_MAX_WORKERS` knobs and memory guidance — worth reading before setting a memory limit.
