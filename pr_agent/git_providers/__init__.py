from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from importlib import import_module

from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider

_BUILTIN_GIT_PROVIDERS: dict[str, tuple[str, str]] = {
    "github": ("pr_agent.git_providers.github_provider", "GithubProvider"),
    "gitlab": ("pr_agent.git_providers.gitlab_provider", "GitLabProvider"),
    "bitbucket": ("pr_agent.git_providers.bitbucket_provider", "BitbucketProvider"),
    "bitbucket_server": ("pr_agent.git_providers.bitbucket_server_provider", "BitbucketServerProvider"),
    "azure": ("pr_agent.git_providers.azuredevops_provider", "AzureDevopsProvider"),
    "codecommit": ("pr_agent.git_providers.codecommit_provider", "CodeCommitProvider"),
    "local": ("pr_agent.git_providers.local_git_provider", "LocalGitProvider"),
    "gerrit": ("pr_agent.git_providers.gerrit_provider", "GerritProvider"),
    "gitea": ("pr_agent.git_providers.gitea_provider", "GiteaProvider"),
    "plain-diff": ("pr_agent.git_providers.plain_diff_provider", "PlainDiffGitProvider"),
}
_PROVIDER_CLASS_NAMES = {class_name: provider_id for provider_id, (_, class_name) in _BUILTIN_GIT_PROVIDERS.items()}
_PROVIDER_EXTRAS = {
    "github": "github",
    "gitlab": "gitlab",
    "bitbucket": "bitbucket",
    "bitbucket_server": "bitbucket",
    "azure": "azure",
    "codecommit": "codecommit",
    "gitea": "gitea",
}


class _LazyGitProviderRegistry(MutableMapping[str, type[GitProvider]]):
    """Mapping-compatible registry that imports built-in providers only when accessed."""

    def __init__(self, builtins: dict[str, tuple[str, str]]):
        self._builtins = dict(builtins)
        self._providers: dict[str, type[GitProvider]] = {}

    def _load_builtin(self, provider_id: str) -> type[GitProvider]:
        module_name, class_name = self._builtins[provider_id]
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as e:
            missing_module = e.name or "unknown dependency"
            extra = _PROVIDER_EXTRAS.get(provider_id)
            install_hint = (
                f"Install it with `pip install 'pr-agent[{extra}]'` before selecting this provider."
                if extra
                else "Install the dependencies required by that provider before selecting it."
            )
            raise ImportError(
                f"Git provider {provider_id!r} could not be loaded because module {missing_module!r} is not installed. "
                f"{install_hint}"
            ) from e

        provider_class = getattr(module, class_name)
        if not (isinstance(provider_class, type) and issubclass(provider_class, GitProvider)):
            raise TypeError(
                f"Built-in git provider {provider_id!r} must be a GitProvider subclass, got {provider_class!r}"
            )

        self._providers[provider_id] = provider_class
        return provider_class

    def __getitem__(self, provider_id: str) -> type[GitProvider]:
        if provider_id in self._providers:
            return self._providers[provider_id]
        if provider_id in self._builtins:
            return self._load_builtin(provider_id)
        raise KeyError(provider_id)

    def __setitem__(self, provider_id: str, provider_class: type[GitProvider]) -> None:
        self._providers[provider_id] = provider_class

    def __delitem__(self, provider_id: str) -> None:
        found = False
        if provider_id in self._providers:
            del self._providers[provider_id]
            found = True
        if provider_id in self._builtins:
            del self._builtins[provider_id]
            found = True
        if not found:
            raise KeyError(provider_id)

    def __iter__(self) -> Iterator[str]:
        yield from self._builtins
        for provider_id in self._providers:
            if provider_id not in self._builtins:
                yield provider_id

    def __len__(self) -> int:
        return len(set(self._builtins) | set(self._providers))

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._providers or provider_id in self._builtins


_GIT_PROVIDERS: MutableMapping[str, type[GitProvider]] = _LazyGitProviderRegistry(_BUILTIN_GIT_PROVIDERS)


def __getattr__(name: str):
    """Preserve package-level provider class imports without eagerly importing every provider module."""
    provider_id = _PROVIDER_CLASS_NAMES.get(name)
    if provider_id is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    provider_class = _GIT_PROVIDERS[provider_id]
    globals()[name] = provider_class
    return provider_class


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_PROVIDER_CLASS_NAMES))


def register_git_provider(provider_id: str, provider_class: type[GitProvider]) -> None:
    """Make `provider_class` selectable through `config.git_provider = provider_id`.

    Meant for providers that live outside this package. Registering the same class again is a
    no-op, so an import-time registration may run twice; a different class under an id that is
    already taken raises, so a package cannot silently shadow a built-in provider.
    """
    if not (isinstance(provider_class, type) and issubclass(provider_class, GitProvider)):
        raise TypeError(f"Git provider {provider_id!r} must be a GitProvider subclass, got {provider_class!r}")
    registered = _GIT_PROVIDERS.get(provider_id)
    if registered is not None and registered is not provider_class:
        raise ValueError(f"Git provider {provider_id!r} is already registered to {registered.__name__}")
    _GIT_PROVIDERS[provider_id] = provider_class


def get_git_provider():
    try:
        provider_id = get_settings().config.git_provider
    except AttributeError as e:
        raise ValueError("git_provider is a required attribute in the configuration file") from e
    # Same plain-diff keying as get_git_provider_with_context(): tools that build
    # their provider through this function (e.g. PRQuestions for `ask`) must also
    # honor loaded diff content, so an extra/repo config that overwrote
    # config.git_provider can't route a supported command to a hosted provider.
    if get_settings().get("plain_diff.content", None):
        provider_id = "plain-diff"
    if provider_id not in _GIT_PROVIDERS:
        raise ValueError(f"Unknown git provider: {provider_id}")
    return _GIT_PROVIDERS[provider_id]


def get_git_provider_with_context(pr_url) -> GitProvider:
    """
    Get a GitProvider instance for the given PR URL. If the GitProvider instance is already in the context, return it.
    """

    is_context_env = None
    try:
        is_context_env = context.get("settings", None)
    except Exception:
        pass  # we are not in a context environment (CLI)

    # check if context["git_provider"][pr_url] exists
    if is_context_env and context.get("git_provider", {}).get(pr_url):
        git_provider = context["git_provider"][pr_url]
        # possibly check if the git_provider is still valid, or if some reset is needed
        # ...
        return git_provider
    else:
        try:
            provider_id = get_settings().config.git_provider
            # Plain-diff mode is keyed on loaded diff content; it must not be
            # overridden by an extra/repo config file that sets a different
            # git_provider (apply_repo_settings merges those before this call).
            if get_settings().get("plain_diff.content", None):
                provider_id = "plain-diff"
            if provider_id not in _GIT_PROVIDERS:
                raise ValueError(f"Unknown git provider: {provider_id}")
            git_provider = _GIT_PROVIDERS[provider_id](pr_url)
            if is_context_env:
                context["git_provider"] = {pr_url: git_provider}
            return git_provider
        except Exception as e:
            raise ValueError(f"Failed to get git provider for {pr_url}") from e
