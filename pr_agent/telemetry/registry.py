from pr_agent.log import get_logger


class _ProviderRegistry:
    """The providers pr-agent created — the only ones its flush/shutdown may touch."""

    def __init__(self):
        self._providers = []

    def __len__(self):
        return len(self._providers)

    def register(self, provider):
        self._providers.append(provider)

    def flush_all(self, timeout_millis=3000):
        for provider in list(self._providers):
            try:
                provider.force_flush(timeout_millis)
            except Exception as e:
                get_logger().warning(f"Error flushing telemetry: {e}")

    def shutdown_all(self):
        for provider in list(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                get_logger().warning(f"Error shutting down telemetry: {e}")
        self._providers.clear()

    def reset(self):
        """Forget registrations without shutting anything down (test seam)."""
        self._providers.clear()


provider_registry = _ProviderRegistry()
