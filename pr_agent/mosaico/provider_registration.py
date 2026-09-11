"""Idempotent registration of DiffInputProvider into pr-agent's provider registry.

Importing this module registers the "mosaico_diff" provider. Only the MOSAICO server imports
it, so the registry is untouched on every other code path. Importing it again is a no-op; if
"mosaico_diff" is already bound to a different class, the import raises rather than keeping it."""
from pr_agent.git_providers import register_git_provider
from pr_agent.mosaico.diff_provider import DiffInputProvider

register_git_provider("mosaico_diff", DiffInputProvider)
