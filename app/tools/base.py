from abc import ABC, abstractmethod

# Read-only work that awaits. Cancellation is cooperative: wait_for stops the
# task only at an await. A blocking call inside this policy keeps running, so
# blocking work must not use it.
ASYNC_READ_ONLY = "async_read_only"

# Read-only work that may block. It runs in a separate process, never on the
# server event loop. The process group can be killed when the timeout expires.
BLOCKING_READ_ONLY = "blocking_read_only"

# Work that can change state. It does not run unless the caller confirms it,
# and even then only inside a killable process. The public API does not confirm.
SIDE_EFFECT = "side_effect"


class Tool(ABC):
    """A registered capability. Tools do not receive memory or permission services."""

    name: str
    description: str
    input_schema: dict
    category: str
    requires_confirmation: bool = False
    execution_policy: str = ASYNC_READ_ONLY
    isolation_key: str | None = None
    timeout_seconds: float | None = None
    enabled: bool = True

    def is_enabled(self) -> bool:
        return self.enabled

    @abstractmethod
    async def run(self, arguments: dict) -> dict:
        """Run a validated call and return a JSON-safe object."""
