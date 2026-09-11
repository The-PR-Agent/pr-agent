## Changing a model in PR-Agent

See [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py) for a list of supported models in PR-Agent.
The current default model and fallback tier are defined in the [configuration file](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml).
To use different models, edit these fields in that file:

```toml
[config]
model = "..."
fallback_models = ["..."]
```

To see which of these models actually handled a given PR, enable `config.output_run_details` (see [Additional configurations](./additional_configurations.md#showing-the-agent-run-details)).
To send small pull requests to a cheaper model, see [Routing small pull requests to a cheaper model](#routing-small-pull-requests-to-a-cheaper-model).

For models and environments not from OpenAI, you might need to provide additional keys and other parameters.
You can give parameters via a configuration file, or from environment variables.

!!! note "Model-specific environment variables"
    See [litellm documentation](https://litellm.vercel.app/docs/proxy/quick_start#supported-llms) for the environment variables needed per model, as they may vary and change over time. Our documentation per-model may not always be up-to-date with the latest changes.
    Failing to set the needed keys of a specific model will usually result in litellm not identifying the model type, and failing to utilize it.

!!! warning "Credential isolation boundary"
    PR-Agent captures request settings and supported provider environment values per handler. Deployment-owned LiteLLM secret managers are outside this request-isolation boundary; their credentials are not snapshotted by PR-Agent. Keep process environment variables and LiteLLM globals stable while requests run. The handler does not isolate arbitrary changes made by embedding applications during a request. Use separate processes when workloads require different deployment-owned credential sources or mutable global authentication and routing state.

    Process-wide routing fallbacks such as `litellm.api_base`, `litellm.api_version`, `litellm.organization`, `litellm.vertex_project`, and `litellm.vertex_location` can be rejected even when unchanged. Use the corresponding PR-Agent settings (`OPENAI.API_BASE`, `OPENAI.API_VERSION`, `OPENAI.ORG`, `VERTEXAI.VERTEX_PROJECT`, and `VERTEXAI.VERTEX_LOCATION`) or supported provider environment variables, and clear the corresponding LiteLLM globals even when they match the intended routing. Use `LITELLM.EXTRA_HEADERS` instead of `litellm.headers`.

### OpenAI like API

To use an OpenAI like API, set the following in your `.secrets.toml` file:

```toml
[openai]
api_base = "https://api.openai.com/v1"
api_key = "sk-..."
```

or use the environment variables (make sure to use double underscores `__`):

```bash
OPENAI__API_BASE=https://api.openai.com/v1
OPENAI__KEY=sk-...
```

### OpenAI Flex Processing

To reduce costs for non-urgent/background tasks, enable Flex Processing:

```toml
[litellm]
extra_body='{"service_tier": "flex"}'
```

See [OpenAI Flex Processing docs](https://platform.openai.com/docs/guides/flex-processing) for details.

### Azure

To use Azure, set in your `.secrets.toml` (working from CLI), or in the GitHub `Settings > Secrets and variables` (working from GitHub App or GitHub Action):

```toml
[openai]
key = "" # your azure api key
api_type = "azure"
api_version = '2023-05-15'  # Check Azure documentation for the current API version
api_base = ""  # The base URL for your Azure OpenAI resource. e.g. "https://<your resource name>.openai.azure.com"
deployment_id = ""  # The deployment name you chose when you deployed the engine
```

and set in your configuration file:

```toml
[config]
model="" # the OpenAI model you've deployed on Azure (e.g. gpt-4o)
fallback_models=["..."]
```

To use Azure AD (Entra id) based authentication set in your `.secrets.toml` (working from CLI), or in the GitHub `Settings > Secrets and variables` (working from GitHub App or GitHub Action):

```toml
[azure_ad]
client_id = ""  # Your Azure AD application client ID
client_secret = ""  # Your Azure AD application client secret
tenant_id = ""  # Your Azure AD tenant ID
api_base = ""  # Your Azure OpenAI service base URL (e.g., https://openai.xyz.com/)
```

The request-local Azure OIDC bridge captures `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_AUTHORITY_HOST`, `AZURE_SCOPE`, and competing `AZURE_CLIENT_SECRET` / `AZURE_USERNAME` / `AZURE_PASSWORD` settings when the handler is initialized. It preserves native authentication precedence while binding companion credentials and their cache identity to the captured authority; an absent authority uses the Azure public-cloud default, while an empty authority is rejected for companion credentials. LiteLLM's native assertion and secret-selector resolvers still control updates and source selection; their underlying deployment-owned credential chains are not request-isolated. Keep those sources scoped to the intended workload identity, or use separate processes for distinct identities.

For native Azure SDK routes other than Cloudflare gateways, ordinary AD tokens use the same captured companion settings and SDK client-cache isolation. Azure Responses routes also bind ordinary AD companion selection to the captured settings. Complete companion credentials can also be selected without an initial AD token, including on native Azure AI raw HTTP routes. Captured companion providers retain callable token refresh and native authentication precedence. If SDK initialization or Responses token resolution reaches LiteLLM's implicit credential discovery with `enable_azure_ad_token_refresh=True`, the request is rejected: that fallback re-reads process-wide identity settings. Configure complete client-secret or username/password companion credentials instead. Managed identity, certificate, and default credential-chain discovery through this fallback are intentionally unsupported; an already cached, isolated SDK client can still be reused without entering discovery.

Passing custom headers to the underlying LLM Model API can be done by setting extra_headers parameter to litellm.

```toml
[litellm]
extra_headers='{"projectId": "<authorized projectId >", ...}') #The value of this setting should be a JSON string representing the desired headers, a ValueError is thrown otherwise.
```

This enables users to pass authorization tokens or API keys, when routing requests through an API management gateway.

Requests that would otherwise inherit non-empty process-wide `litellm.headers` are rejected, even for non-authentication headers; configure headers through `LITELLM.EXTRA_HEADERS` instead.

### Ollama

You can run models locally through either [VLLM](https://docs.litellm.ai/docs/providers/vllm) or [Ollama](https://docs.litellm.ai/docs/providers/ollama)

E.g. to use a new model locally via Ollama, set in `.secrets.toml` or in a configuration file:

```toml
[config]
model = "ollama/qwen2.5-coder:32b"
fallback_models=["ollama/qwen2.5-coder:32b"]
custom_model_max_tokens=128000 # set the maximal input tokens for the model
duplicate_examples=true # will duplicate the examples in the prompt, to help the model to generate structured output

[ollama]
api_base = "http://localhost:11434" # or whatever port you're running Ollama on
```

By default, Ollama uses a context window size of 2048 tokens. In most cases this is not enough to cover pr-agent prompt and pull-request diff. Context window size can be overridden with the `OLLAMA_CONTEXT_LENGTH` environment variable. For example, to set the default context length to 8K, use: `OLLAMA_CONTEXT_LENGTH=8192 ollama serve`. More information you can find on the [official ollama faq](https://docs.ollama.com/faq#how-can-i-specify-the-context-window-size).

Please note that the `custom_model_max_tokens` setting should be configured in accordance with the `OLLAMA_CONTEXT_LENGTH`. Failure to do so may result in unexpected model output.

!!! note "Local models vs commercial models"
    PR-Agent is compatible with almost any AI model, but analyzing complex code repositories and pull requests requires a model specifically optimized for code analysis.

    Commercial models such as GPT-5, Claude Sonnet, and Gemini have demonstrated robust capabilities in generating structured output for code analysis tasks with large input. In contrast, most open-source models currently available (as of January 2025) face challenges with these complex tasks.

    Based on our testing, local open-source models are suitable for experimentation and learning purposes (mainly for the `ask` command), but they are not suitable for production-level code analysis tasks.

    Hence, for production workflows and real-world usage, we recommend using commercial models.

### Hugging Face

To use a new model with Hugging Face Inference Endpoints, for example, set:

```toml
[config] # in configuration.toml
model = "huggingface/meta-llama/Llama-2-7b-chat-hf"
fallback_models=["huggingface/meta-llama/Llama-2-7b-chat-hf"]
custom_model_max_tokens=... # set the maximal input tokens for the model

[huggingface] # in .secrets.toml
key = ... # your Hugging Face api key
api_base = ... # the base url for your Hugging Face inference endpoint
```

(you can obtain a Llama2 key from [here](https://replicate.com/replicate/llama-2-70b-chat/api))

### Replicate

To use Llama2 model with Replicate, for example, set:

```toml
[config] # in configuration.toml
model = "replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1"
fallback_models=["replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1"]
[replicate] # in .secrets.toml
key = ...
```

(you can obtain a Llama2 key from [here](https://replicate.com/replicate/llama-2-70b-chat/api))

Also, review the [.secrets_template.toml](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/.secrets_template.toml) file for instructions on how to set keys for other models.

### Groq

To use Llama3 model with Groq, for example, set:

```toml
[config] # in configuration.toml
model = "llama3-70b-8192"
fallback_models = ["groq/llama3-70b-8192"]
[groq] # in .secrets.toml
key = ... # your Groq api key
```

(you can obtain a Groq key from [here](https://console.groq.com/keys))

### SambaNova

To use MiniMax-M3 model with SambaNova, for example, set:

```toml
[config] # in configuration.toml
model = "sambanova/MiniMax-M3"
fallback_models = ["sambanova/MiniMax-M2.7"]
[sambanova] # in .secrets.toml
key = ... # your SambaNova api key
```

(you can obtain a SambaNova key from [here](https://cloud.sambanova.ai/apis))

### xAI

To use xAI's models with PR-Agent, set:

```toml
[config] # in configuration.toml
model = "xai/grok-4.6"
fallback_models = ["xai/grok-4.6"] # or any other model as fallback

[xai] # in .secrets.toml
key = "..." # your xAI API key
```

You can obtain an xAI API key from [xAI's console](https://console.x.ai/) by creating an account and navigating to the developer settings page.

Grok 4.5 and Grok 4.6 are registered with a 500K token context window (`xai/grok-4.5`, `xai/grok-4.5-latest`, `xai/grok-build-latest`, `xai/grok-4.6`, `openrouter/x-ai/grok-4.5`, `openrouter/x-ai/grok-4.6`). xAI publishes `grok-4.5-latest` and `grok-build-latest` aliases for Grok 4.5; Grok 4.6 currently has no published alias.

Grok 4.5 and Grok 4.6 are always-on reasoning models and honor `config.reasoning_effort` (`low`, `medium`, `high`; `"xhigh"` is supported on Grok 4.6 and later). PR-Agent sends `medium` by default; set `"high"` to restore xAI's native default. This setting is global, so changing it also affects other registered reasoning models. Unsupported values are clamped to the closest accepted level (`none`/`minimal` → `"low"`; `"max"`/`"xhigh"` on Grok 4.5 → `"high"`; `"max"` on Grok 4.6 → `"xhigh"`).

OpenRouter routes (`openrouter/x-ai/grok-4.5`, `openrouter/x-ai/grok-4.6`) apply the same clamping after the OpenRouter effort value is resolved but before the none-versus-budget decision. An explicit `openrouter.reasoning_effort` overrides the global effort; a positive `openrouter.reasoning_max_tokens` remains budget-only and suppresses effort, including a clamped `"none"` value. Routing suffixes such as `:nitro` require `custom_model_max_tokens` because token lookup currently uses exact model IDs.

### Vertex AI

To use Google's Vertex AI platform and its associated models (chat-bison/codechat-bison) set:

```toml
[config] # in configuration.toml
model = "vertex_ai/codechat-bison"
fallback_models="vertex_ai/codechat-bison"

[vertexai] # in .secrets.toml
vertex_project = "my-google-cloud-project"
vertex_location = ""
```

Your [application default credentials](https://cloud.google.com/docs/authentication/application-default-credentials) will be used for authentication so there is no need to set explicit credentials in most environments.

If you do want to set explicit credentials, then you can use the `GOOGLE_APPLICATION_CREDENTIALS` environment variable set to a path to a json credentials file.

Each handler captures the selected Cloud SDK ADC file and its resource project configuration, including `CLOUDSDK_CONFIG`, the active named configuration, and `CLOUDSDK_CORE_PROJECT`. Later changes apply to new handlers, not existing ones. The quota project remains separate from the resource project. When no ADC file exists at initialization, the handler retains managed-runtime discovery rather than adopting a file created later.

AWS-backed Vertex workload identity federation captures environment credentials and region per handler. Different captured identities use separate credential caches even when they share the same WIF configuration; identical snapshots can reuse a cache entry. Metadata-backed credentials continue refreshing through the captured metadata source.

Vertex external-account credentials with an executable source are intentionally unsupported, including when `GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1`. The helper and its cached output can change identity after a handler captures its configuration, so Vertex requests reject this source before using it for authentication. Use a non-executable credential source configured for the intended identity instead; normal credential refresh remains enabled for supported sources.

### Google AI Studio

To use [Google AI Studio](https://aistudio.google.com/) models, set the relevant models in the configuration section of the configuration file:

```toml
[config] # in configuration.toml
model="gemini/gemini-1.5-flash"
fallback_models=["gemini/gemini-1.5-flash"]

[google_ai_studio] # in .secrets.toml
gemini_api_key = "..."
```

If you don't want to set the API key in the .secrets.toml file, you can set the `GOOGLE_AI_STUDIO.GEMINI_API_KEY` environment variable.

### Anthropic

To use Anthropic models, set the relevant models in the configuration section of the configuration file:

```toml
[config]
model="anthropic/claude-3-opus-20240229"
fallback_models=["anthropic/claude-3-opus-20240229"]
```

And also set the api key in the .secrets.toml file:

```toml
[anthropic]
KEY = "..."
```

See [litellm](https://docs.litellm.ai/docs/providers/anthropic#usage) documentation for more information about the environment variables required for Anthropic.

### Amazon Bedrock

To use Amazon Bedrock and its foundational models, add the below configuration:

```toml
[config] # in configuration.toml
model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
fallback_models=["bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"]

[aws]
AWS_ACCESS_KEY_ID="..."
AWS_SECRET_ACCESS_KEY="..."
AWS_REGION_NAME="..."
```

You can also use the new Meta Llama 4 models available on Amazon Bedrock:

```toml
[config] # in configuration.toml
model="bedrock/us.meta.llama4-scout-17b-instruct-v1:0"
fallback_models=["bedrock/us.meta.llama4-maverick-17b-instruct-v1:0"]
```

Grok 4.3 is available through Amazon Bedrock Mantle rather than the classic Bedrock runtime:

```toml
[config] # in configuration.toml
model="bedrock_mantle/xai.grok-4.3"
fallback_models=["bedrock_mantle/xai.grok-4.3"]
```

Bedrock Mantle uses the same AWS credential sources, but its IAM permissions differ from the classic runtime. See
the [AWS Mantle inference permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference.html).

#### Using IAM Role Credentials (Recommended on AWS Compute)

When running PR-Agent on AWS infrastructure (EC2, ECS/Fargate, EKS with IRSA, Lambda, or any self-hosted GitHub Actions runner on AWS), the instance or task already has an IAM role attached. You can use those ambient credentials directly instead of storing long-lived static keys.

Set `AWS_USE_IMDS=true` in the environment. PR-Agent will resolve credentials via boto3's standard provider chain, which handles all AWS compute contexts transparently:

| Compute context | Mechanism |
|---|---|
| EC2 instance with IAM role | IMDSv2 (169.254.169.254) |
| ECS / Fargate task role | Task metadata endpoint |
| EKS pod with IRSA | Web identity token + STS |
| Lambda function | Runtime-injected credentials |

Credential discovery runs synchronously when the handler is initialized. Before each SigV4 call, PR-Agent
refreshes credentials synchronously through the same boto3 credentials object and passes a request-local snapshot
to LiteLLM, without writing credentials into the process environment. AWS calls using this provider chain are
serialized within a handler, including any static-credential retry. Discovery and refresh can block the event loop.

The same opt-in is required for other boto3 provider-chain sources, including `AWS_PROFILE` and shared credentials
files. LiteLLM-specific `AWS_PROFILE_NAME` and `AWS_ROLE_NAME` selectors are not supported because they can override
request-local credentials; unset them and use `AWS_USE_IMDS=true` instead.

When upgrading from implicit LiteLLM credential-chain discovery, set `AWS_USE_IMDS=true` explicitly. Without this
opt-in, PR-Agent only uses complete static credentials from settings or environment credentials captured when the
handler is initialized; it does not ask LiteLLM to discover a role or profile at request time.

For classic Bedrock, a supported model ARN supplies the request region ahead of environment/settings regions, including during static-credential retries. Converse also uses a captured `litellm.model_id` ARN, or a region/model path when `model_id` is absent; Invoke does not derive its region from the separate `model_id`. Otherwise, without `AWS_USE_IMDS=true`, set `aws.AWS_REGION_NAME`, `AWS_REGION_NAME`, `AWS_REGION`, or `AWS_DEFAULT_REGION`. Complete environment credentials alone do not enable region discovery from boto3 profiles or LiteLLM's default region. Static credentials in `[aws]` still require `AWS_REGION_NAME`.

For Bedrock Mantle without IMDS or complete static credentials, the region comes from `BEDROCK_MANTLE_REGION`, `AWS_REGION_NAME`, `aws.AWS_REGION_NAME`, or `AWS_REGION`, then defaults to `us-east-1`; `AWS_DEFAULT_REGION` alone does not change that default. A `BEDROCK_MANTLE_API_BASE` on the standard host `https://bedrock-mantle.<region>.api.aws` supplies the region and overrides these sources; a custom endpoint host does not. Complete static credentials and opted-in boto3 discovery retain the AWS region policy described above.

Without `AWS_USE_IMDS=true`, environment authentication follows LiteLLM's `AWS_SESSION_TOKEN` selection. The legacy `AWS_SECURITY_TOKEN` alias is recognized only by the opted-in boto3 chain, which prefers it over `AWS_SESSION_TOKEN` when both are nonempty.

Bedrock bearer tokens must come from handler-captured credentials rather than process-wide LiteLLM fallback, and `AWS_BEARER_TOKEN_BEDROCK` must be unset for `sagemaker_chat` and `sagemaker_nova` routes.

When resolving credentials through profiles with `AWS_USE_IMDS=true`, PR-Agent does not execute
`credential_process` in the selected profile or its `source_profile` chain. If such a process is configured,
PR-Agent uses complete static credentials from `[aws]` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`AWS_REGION_NAME`, plus `AWS_SESSION_TOKEN` when required) instead; without them, credential resolution fails.
This restriction does not apply when using complete environment credentials captured at handler initialization.

Workload token files selected by `AWS_WEB_IDENTITY_TOKEN_FILE`, profile `web_identity_token_file`, or
`AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` must be controlled by the deployment, not switched between tenants.
PR-Agent retains native token reload and credential refresh from the selected source; it does not freeze token
contents or detect a different identity replacing the contents at the same path. Use separately controlled
credential sources and appropriate process/container isolation for distinct workload identities, or supply
explicit request-local credentials. Request-local configuration is not an isolation boundary against arbitrary
code running in the same process.

Minimal GitHub Actions workflow (no AWS secret keys required):

```yaml
- uses: the-pr-agent/pr-agent@main
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    AWS_USE_IMDS: "true"
    # AWS_REGION_NAME: us-east-1  # optional if the instance metadata provides it
  with:
    command: review
```

The IAM role must have `bedrock:InvokeModel` permission on the target model ARN, for example:

```json
{
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20240620-v1:0"
}
```

If you also configure static keys in `[aws]`, they serve as an automatic fallback when ambient credentials cannot be resolved or a SigV4 call using the opted-in provider chain raises an API error other than a rate-limit error. This applies to Bedrock, Bedrock Mantle, and SageMaker routes and preserves the existing fallback behavior for IAM authorization and connection failures, using only the static credentials captured by the handler.

#### Custom Inference Profiles

To invoke an [application inference profile](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-application-inference-profiles.html) with a classic `bedrock/` model (for cost allocation tags and other configuration settings), set `model_id` to the profile ARN in your configuration:

```toml
[config] # in configuration.toml
model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
fallback_models=["bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"]

[aws]
AWS_ACCESS_KEY_ID="..."
AWS_SECRET_ACCESS_KEY="..."
AWS_REGION_NAME="..."

[litellm]
model_id = "your-application-inference-profile-arn"
```

The `litellm.model_id` parameter applies only to classic `bedrock/` calls made through the `bedrock-runtime` APIs. It does not apply to `bedrock_mantle/`; for cost allocation with the Mantle Chat Completions and Responses APIs, use [Amazon Bedrock Projects](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-projects.html).

#### Claude 5 thinking with an application inference profile ARN

Claude Sonnet 5 on Bedrock is invoked through an inference profile rather than a direct
foundation-model id. When that profile is an application inference profile, its ARN is an
opaque value that carries no model name. Thinking configuration in PR-Agent is gated on
recognizing the model, so the opaque ARN can never match: `enable_claude_adaptive_thinking`
requires a recognized Claude 5 model name in the id, and `enable_claude_extended_thinking`
requires exact membership in `claude_extended_thinking_models`. PR-Agent logs a warning in
that case, so the unconfigured state is no longer silent.

Address the model by name and pass the profile ARN through `litellm.model_id`, which is what
the invocation actually uses:

```toml
[config] # in configuration.toml
model = "bedrock/converse/eu.anthropic.claude-sonnet-5"
fallback_models = ["bedrock/converse/eu.anthropic.claude-sonnet-5"]
enable_claude_adaptive_thinking = true # requires a recognizable claude 5 model name in `model`

[litellm]
model_id = "arn:aws:bedrock:eu-central-1:<account-id>:application-inference-profile/<profile-id>"
```

Cost attribution is preserved through the application inference profile, and because `model`
is the named id, the adaptive-thinking payload is applied and kept intact.

Two caveats. First, ARNs only fail the detection when the suffix is opaque: an ARN that
embeds the model family, for example `...:inference-profile/us.anthropic.claude-sonnet-5`,
normalises to a string the adaptive regex does match. The miss is specific to application
inference profiles with an opaque hex suffix. Second, `litellm.model_id` is a single global
value applied to every model whose id contains `bedrock/`, so this configuration cannot point
different models at different profiles within one fallback chain without per-call handling.

#### Using a Custom VPC Endpoint (PrivateLink)

To route Bedrock traffic through a VPC interface endpoint instead of the public `bedrock-runtime` endpoint, set `AWS_BEDROCK_RUNTIME_ENDPOINT` either as an environment variable or in `[aws]`:

```toml
[aws]
AWS_BEDROCK_RUNTIME_ENDPOINT="https://bedrock-runtime.us-east-1.amazonaws.com"
```

See [litellm](https://docs.litellm.ai/docs/providers/bedrock#usage) documentation for more information about the environment variables required for Amazon Bedrock.

### DeepSeek

To use deepseek-v4 model with DeepSeek, for example, set:

```toml
[config] # in configuration.toml
model = "deepseek/deepseek-v4-pro"
fallback_models=["deepseek/deepseek-v4-flash"]
```

and fill up your key

```toml
[deepseek] # in .secrets.toml
key = ...
```

(you can obtain a deepseek-v4 key from [here](https://platform.deepseek.com/api_keys))

### GLM (Z.AI)

To use GLM models with Z.AI (Zhipu), for example, set:

```toml
[config] # in configuration.toml
model = "zai/glm-5.2"
fallback_models=["zai/glm-5.2"]
```

and fill up your key

```toml
[zai] # in .secrets.toml
key = ...
```

(you can obtain a Z.AI API key from [here](https://z.ai/))

### Kimi (Moonshot)

To use Kimi models with Moonshot, for example, set:

```toml
[config] # in configuration.toml
model = "moonshot/kimi-k3"
fallback_models=["moonshot/kimi-k3"]
```

and fill up your key

```toml
[moonshot] # in .secrets.toml
key = ...
```

(you can obtain a Moonshot API key from [here](https://platform.moonshot.ai/))

If you are on the China endpoint instead, add `api_base = "https://api.moonshot.cn/v1"` under `[moonshot]`.

### Qwen (DashScope)

To use Qwen models with Alibaba DashScope, for example, set:

```toml
[config] # in configuration.toml
model = "dashscope/qwen3.8-max"
fallback_models=["dashscope/qwen3.8-max"]
```

and fill up your key

```toml
[dashscope] # in .secrets.toml
key = ...
```

(you can obtain a DashScope API key from [here](https://dashscope.console.aliyun.com/))

### Xiaomi MiMo

To use Xiaomi MiMo models, for example, set:

```toml
[config] # in configuration.toml
model = "xiaomi_mimo/mimo-v2.5"
fallback_models=["xiaomi_mimo/mimo-v2.5"]
```

and fill up your key

```toml
[xiaomi_mimo] # in .secrets.toml
key = ...
```

(you can obtain a Xiaomi MiMo API key from [here](https://platform.xiaomimimo.com/#/docs))

### DeepInfra

To use DeepSeek model with DeepInfra, for example, set:

```toml
[config] # in configuration.toml
model = "deepinfra/deepseek-ai/DeepSeek-R1-Distill-Llama-70B"
fallback_models = ["deepinfra/deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"]
[deepinfra] # in .secrets.toml
key = ... # your DeepInfra api key
```

(you can obtain a DeepInfra key from [here](https://deepinfra.com/dash/api_keys))

### Mistral

To use models like Mistral or Codestral with Mistral, for example, set:

```toml
[config] # in configuration.toml
model = "mistral/mistral-small-latest"
fallback_models = ["mistral/mistral-medium-latest"]
[mistral] # in .secrets.toml
key = "..." # your Mistral api key
```

(you can obtain a Mistral key from [here](https://console.mistral.ai/api-keys))

### Codestral

To use Codestral model with Codestral, for example, set:

```toml
[config] # in configuration.toml
model = "codestral/codestral-latest"
fallback_models = ["codestral/codestral-2405"]
[codestral] # in .secrets.toml
key = "..." # your Codestral api key
```

(you can obtain a Codestral key from [here](https://console.mistral.ai/codestral))

### Databricks

To use a model hosted on Databricks (e.g. an Azure Databricks serving endpoint), set:

```toml
[config] # in configuration.toml
model = "databricks/databricks-claude-sonnet-4"
fallback_models = ["databricks/databricks-claude-sonnet-4"]
[databricks] # in .secrets.toml
api_key = "..." # your Databricks personal access token (PAT)
api_base = "https://adb-xxxx.azuredatabricks.net/serving-endpoints" # your workspace serving-endpoints URL
```

The model name after the `databricks/` prefix is the name of your serving endpoint. See LiteLLM's [Databricks provider docs](https://docs.litellm.ai/docs/providers/databricks) for details.

The configured PAT and endpoint are captured per handler. When both `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` are set, LiteLLM performs an OAuth M2M exchange even if a PAT is supplied. An exchange failure can abort the request; after a successful exchange, the supplied PAT replaces the OAuth token in the final Authorization header. Without OAuth M2M credentials or a PAT, optional Databricks SDK authentication remains available. These deployment-owned credentials and profile selection are not request-isolated. Do not change those sources between tenants in a shared process; use request-specific PATs with OAuth M2M credentials unset, or separate processes for distinct workload identities.

### Openrouter

To use model from Openrouter, for example, set:

```toml
[config] # in configuration.toml
model="openrouter/anthropic/claude-3.7-sonnet"
fallback_models=["openrouter/deepseek/deepseek-chat"]
custom_model_max_tokens=20000

[openrouter]  # in .secrets.toml or passed an environment variable openrouter__key
key = "..." # your openrouter api key
```

(you can obtain an Openrouter API key from [here](https://openrouter.ai/settings/keys))

OpenRouter's router models can be selected directly without setting `custom_model_max_tokens`:

```toml
[config]
model = "openrouter/auto"
fallback_models = ["openrouter/free"]
```

PR-Agent also registers `openrouter/fusion` and `openrouter/pareto-code`. Provider routing, reasoning, and output-cap
settings are optional for all four router models; omit them to use OpenRouter's defaults. See OpenRouter's documentation
for the [Auto](https://openrouter.ai/docs/guides/routing/routers/auto-router),
[Free](https://openrouter.ai/docs/guides/routing/routers/free-router),
[Fusion](https://openrouter.ai/docs/guides/routing/routers/fusion-router), and
[Pareto](https://openrouter.ai/docs/guides/routing/routers/pareto-router) routers.

#### Openrouter provider routing, reasoning and output cap

For `openrouter/...` models you can optionally restrict which upstream providers Openrouter uses, control reasoning, and cap the completion length. All keys live in the `[openrouter]` section of `configuration.toml`. Models listed in [`SUPPORT_REASONING_EFFORT_MODELS`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py) inherit `config.reasoning_effort` unless an Openrouter-specific effort or token budget is set.

```toml
[openrouter]
# Uncomment and adjust the keys you need; unset keys keep Openrouter's defaults.
# provider_only = ["z-ai"]             # hard allowlist of upstream providers; empty = default routing
# provider_order = ["z-ai", "novita"]  # preferred order instead of an allowlist; ignored when provider_only is set
# allow_fallbacks = true               # when provider_order is set, allow routing beyond the list
# reasoning_effort = "low"             # override global effort: "none", "minimal", "low", "medium", "high", "xhigh" or "max"
# reasoning_max_tokens = 2048          # explicit budget; ignored only when final effort remains "none"
# max_tokens = 16000                   # hard cap on completion tokens for the request
```

`provider_only` and `reasoning_effort = "none"` are useful to pin a specific provider and to bound the cost of reasoning models. Because Openrouter treats effort and token budgets as mutually exclusive, an explicit Openrouter-specific `"none"` keeps reasoning disabled when the model supports disabling it. Grok 4.5/4.6 clamp `"none"` before precedence is applied, so a positive budget wins there; otherwise a positive `reasoning_max_tokens` value takes precedence over the global effort and other Openrouter-specific values. Invalid Openrouter-specific effort values are warned about and treated as unset, so registered reasoning models fall back to `config.reasoning_effort`. Openrouter normalizes `"max"` to `"xhigh"` in this path for LiteLLM/OpenRouter compatibility. Supported effort values vary by model, and models whose metadata marks reasoning as mandatory reject `"none"`. For Anthropic models using a reasoning budget, set the effective output `max_tokens` higher than `reasoning_max_tokens` so the final answer has output headroom. See the Openrouter [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection) and [reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) docs.

### OrcaRouter

[OrcaRouter](https://www.orcarouter.ai) is an OpenAI-compatible AI gateway. It needs no provider-specific code in PR-Agent: the `openai/` prefix routes the request to OrcaRouter's base URL through litellm's OpenAI-compatible path, the same way the [Neon AI Gateway](#neon-ai-gateway) is handled.

To use a model through OrcaRouter, set:

```toml
[config] # in configuration.toml
model = "openai/anthropic/claude-fable-5"
fallback_models = ["openai/auto"]
custom_model_max_tokens = 20000

[openai] # in .secrets.toml
api_base = "https://api.orcarouter.ai/v1"
key = "..." # your OrcaRouter api key
```

or use the environment variables (make sure to use double underscores `__`):

```bash
OPENAI__API_BASE=https://api.orcarouter.ai/v1
OPENAI__KEY=...
```

(you can obtain an OrcaRouter API key from [here](https://www.orcarouter.ai/register))

Keep the `openai/` prefix on the model name, whatever OrcaRouter model ID you use (`openai/anthropic/claude-fable-5`, `openai/auto`, ...): the prefix routes the request through litellm's OpenAI-compatible path. A prefixed name is not in the `MAX_TOKENS` table [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py), so you also have to set `custom_model_max_tokens`. OrcaRouter governs routing and guardrails itself, but `config.reasoning_effort` still reaches it: PR-Agent matches the last segment of the model ID against `SUPPORT_REASONING_EFFORT_MODELS`, so an ID such as `openai/google/gemini-2.5-pro` or `openai/o3` sends the configured effort (default `"medium"`) even with nothing set. The example IDs above are not in that list and are unaffected.

### Neon AI Gateway

[Neon AI Gateway](https://neon.com/docs/ai-gateway/overview) is an OpenAI-compatible inference gateway. Each Neon branch has its own gateway host, so the base URL points at a single branch and not at an account. Neon publishes that host alongside the credential as `NEON_AI_GATEWAY_BASE_URL`. The value has no path, so append `/v1` to reach chat completions.

To use a model served by a Neon branch, set:

```toml
[config] # in configuration.toml
model = "openai/gpt-5-mini"
fallback_models = ["openai/gpt-5-mini"]
custom_model_max_tokens = 400000 # the context window Neon publishes for the model

[openai] # in .secrets.toml
api_base = "https://<your-neon-branch-host>/v1"
key = "..." # your Neon AI Gateway credential
```

or use the environment variables (make sure to use double underscores `__`):

```bash
OPENAI__API_BASE=https://<your-neon-branch-host>/v1
OPENAI__KEY=...
```

Keep the `openai/` prefix on the model name, whichever Neon model ID you use: the prefix routes the request through litellm's OpenAI-compatible path. A prefixed name is not in the `MAX_TOKENS` table [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py), so you also have to set `custom_model_max_tokens`. Take the value from Neon's [model catalog](https://neon.com/docs/ai-gateway/models).

Create the credential per branch in the [Neon Console](https://console.neon.tech/) with the `ai_gateway:invoke` scope. The credential also works on branches descended from the one it was created on. The gateway is in beta and requires a paid Neon plan. It runs only in AWS US East (Ohio), `aws-us-east-2`.

!!! note "Chat completions only"
    Some model IDs in Neon's catalog are served through the OpenAI Responses API, which Neon exposes under `/openai/v1` instead of `/v1`. The configuration above points at the chat-completions endpoint, so it cannot reach those models. Neon also documents a few models that return `message.content` as an array of typed blocks rather than a string, and PR-Agent reads the reply as a string.

### GitHub Copilot

Models under an active GitHub Copilot subscription are available through litellm's `github_copilot` provider, which authenticates as your GitHub identity rather than with a dedicated API key:

```toml
[config]
model = "github_copilot/gpt-4o"
fallback_models = ["github_copilot/gpt-4.1"]
```

The GitHub identity behind the model needs an active Copilot subscription. The token budget for a Copilot model is resolved automatically from litellm's model metadata (verified against the pinned litellm 1.100.0), so `custom_model_max_tokens` is not required. However, `get_max_tokens` clamps the effective window to `config.max_model_tokens`, which defaults to 32000. To use the full context window of the model (e.g., 64000 for gpt-4o, 128000 for gpt-4.1), raise `config.max_model_tokens` accordingly.

Authentication uses the [GitHub Copilot provider](https://docs.litellm.ai/docs/providers/github_copilot) flow:

1. litellm first looks for a pre-seeded GitHub access token in `access-token` under `GITHUB_COPILOT_TOKEN_DIR` (default `~/.config/litellm/github_copilot`; the filename is overridable with `GITHUB_COPILOT_ACCESS_TOKEN_FILE`). In a CI runner, write that token before the job runs - for example, mount a secret into the directory or point the variable at a mounted secret directory - and the flow never becomes interactive.
2. Only when the file is missing or empty does litellm fall back to the interactive device-code flow (`POST https://github.com/login/device/code`, up to three attempts), which does not suit unattended runners.
3. The Copilot API key (`api-key.json` in the same directory) is refreshed automatically against `https://api.github.com/copilot_internal/v2/token`, using the pre-seeded access token.

Whether Copilot's terms permit this programmatic use is a question for GitHub rather than a guarantee this project can make, so confirm before relying on the route.

### Custom models

If the relevant model doesn't appear [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/algo/__init__.py), you can still use it as a custom model:

1. Set the model name in the configuration file:

```toml
[config]
model="custom_model_name"
fallback_models=["custom_model_name"]
```

2. Set the maximal tokens for the model:

```toml
[config]
custom_model_max_tokens= ...
```

3. Go to [litellm documentation](https://litellm.vercel.app/docs/proxy/quick_start#supported-llms), find the model you want to use, and set the relevant environment variables.

4. Most reasoning models do not support chat-style inputs (`system` and `user` messages) or temperature settings.
   To bypass chat templates and temperature controls, set `config.custom_reasoning_model = true` in your configuration file.

## Dedicated parameters

### OpenAI models

```toml
[config]
reasoning_effort = "medium" # "none", "minimal", "low", "medium", "high", "xhigh", "max"
```

With the OpenAI models that support reasoning effort (eg: gpt-5.6-terra), you can specify its reasoning effort via `config` section. The default value is `medium`. You can change it to any supported value based on your usage. Available values depend on the model and provider.

To use [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra):

```toml
[config]
model = "gpt-6-astra"
reasoning_effort = "medium" # "low", "medium", "high", "xhigh", "max"
```

PR-Agent omits temperature for GPT-6 Astra and maps `none` or `minimal` reasoning effort to `low`.
Its 1,050,000-token context window remains subject to `config.max_model_tokens`.
The existing Chat Completions path is used; access depends on your OpenAI account.

### Anthropic models

```toml
[config]
enable_claude_extended_thinking = false # Set to true to enable extended thinking feature
extended_thinking_budget_tokens = 2048
extended_thinking_max_output_tokens = 4096
```

By default, PR-Agent applies the extended-thinking payload only to a built-in list of Claude models
(see `CLAUDE_EXTENDED_THINKING_MODELS` in `pr_agent/algo/__init__.py`). If you use a newer or custom
Claude model that is not in that list, you can override it:

```toml
[config]
claude_extended_thinking_models_override = ["anthropic/claude-my-new-model"]
```

When `claude_extended_thinking_models_override` is non-empty, it fully replaces the built-in list, so
include every model that should receive extended thinking. Leave it empty (the default) to use the
built-in defaults.

!!! note "Only models that accept a thinking budget are supported"
    PR-Agent enables extended thinking through the manual
    `thinking={"type": "enabled", "budget_tokens": ...}` request. Adaptive-only Claude models
    (e.g. Opus 4.7/4.8, Opus 5, Sonnet 5, Fable 5, Fable 5.1) reject `budget_tokens`, so they are
    intentionally excluded from the built-in defaults. If you add one to
    `claude_extended_thinking_models_override` anyway, PR-Agent skips the extended-thinking payload
    for it and logs a warning rather than sending a request the provider would reject — use
    `enable_claude_adaptive_thinking` for those models instead.

Both thinking gates only fire when the model id itself is recognizable: an opaque id such as a
Bedrock application inference profile ARN matches neither gate, and PR-Agent logs a warning
instead of silently sending nothing. See [Claude 5 thinking with an application inference
profile ARN](#claude-5-thinking-with-an-application-inference-profile-arn).

## Output token limit

```toml
[config]
max_output_tokens = 0 # 0 = unset (default)
```

By default PR-Agent does not send an output token limit on model calls, so the
provider's own default applies. On some providers that default is low — for example, AWS Bedrock
(Converse API) can cap Claude reasoning models at 4096 output tokens, and since reasoning tokens
count against that budget, the visible answer can come back empty or truncated. Set
`config.max_output_tokens` to a positive value (e.g. `16000`) to send it to LiteLLM as
`max_completion_tokens` for GPT-6 Astra or `max_tokens` for other models. Use a value supported by
the selected model; GPT-6 Astra supports at most 128,000 output tokens, including reasoning tokens.
PR-Agent does not automatically clamp this setting to the model's output limit.
When Claude extended thinking is enabled, `extended_thinking_max_output_tokens` takes precedence.
For models with small context windows, keep in mind that prompt and completion tokens share the
model's context window: size `config.max_model_tokens` so the packed prompt leaves room for the
configured output limit.

## Routing small pull requests to a cheaper model

`config.model` handles every pull request, however small. With model routing enabled, a pull request under a
configured size goes to a cheaper model instead, and `config.fallback_models` still apply after it:

```toml
[model_routing]
enable = true

[[model_routing.rules]]
max_hunks = 3
model = "gpt-5.6-luna"

[[model_routing.rules]]
max_hunks = 15
max_files = 6
model = "gpt-5.6-terra"
```

Rules are checked in order, and the first one whose limits the pull request fits selects the primary model for
the call. A pull request that fits no rule uses `config.model`. Size is measured by the number of diff hunks
(`max_hunks`) and changed files (`max_files`) left after the `[ignore]` rules. Every git provider reports both,
and neither depends on a model's tokenizer, so a threshold means the same thing whichever model it selects.

Routing applies only to calls that ask for the regular model: `/review`, `/improve`, `/generate_labels` and
`/add_docs`. Tools that already use `config.model_weak` (`/describe`, `/ask`, `/update_changelog`) are left
alone, and a dedicated `config.model_reasoning` is still used for self-reflection. With `config.output_run_details`
enabled, the run details show which model a routed pull request ended up on.

!!! note "Azure deployments"
    An Azure deployment is tied to one model, so when `openai.deployment_id` is set each rule also needs its own
    `deployment_id`. A rule without one is skipped with a warning and the next rule is tried.
