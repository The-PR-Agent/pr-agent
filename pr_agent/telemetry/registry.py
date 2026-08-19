from pr_agent.log import get_logger


class _ProviderRegistry:
    """Tracks the OpenTelemetry providers pr-agent creates.

    This is the replacement for module-global provider variables: lifecycle
    state lives in one object instead of per-module globals. Request-boundary
    flushes and process shutdown iterate exactly what pr-agent owns — never
    the process-global provider, which may belong to a host application
    embedding pr-agent. Even if a concurrent first call races the cached
    initializers into building two providers, both get registered, so both
    are flushed and shut down.
    """

    def __init__(self):
        self._providers = []

    def __len__(self):
        return len(self._providers)

    def register(self, provider):
        self._providers.append(provider)

    def flush_all(self, timeout_millis=3000):
        """Force-export buffered telemetry; providers stay usable."""
        for provider in list(self._providers):
            try:
                provider.force_flush(timeout_millis)
            except Exception as e:
                get_logger().warning(f"Error flushing telemetry: {e}")

    def shutdown_all(self):
        """Flush and shut down every registered provider, then drop them.

        Shutdown is terminal: releasing the references lets the provider
        object graphs (processors, exporters, buffered data) be reclaimed.
        """
        for provider in list(self._providers):
            try:
                get_logger().debug("Shutting down telemetry provider")
                provider.shutdown()
            except Exception as e:
                get_logger().warning(f"Error shutting down telemetry: {e}")
        self._providers.clear()

    def reset(self):
        """Forget all registrations without shutting anything down (test seam)."""
        self._providers.clear()


# The module is the singleton boundary: Python imports it exactly once per
# process, so this is the one registry. The class stays private — a second
# instance would split lifecycle state across registries.
provider_registry = _ProviderRegistry()
