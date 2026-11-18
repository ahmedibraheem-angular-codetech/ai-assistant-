class ToolError(Exception):
    """Base error for tool registration and execution."""


class DuplicateToolError(ToolError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Tool '{name}' is already registered.")


class ToolNotFoundError(ToolError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Tool '{name}' was not found.")


class UnknownToolNameError(ToolError):
    """A grant named a tool that is not registered."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Tool '{name}' is not registered.")


class ToolDisabledError(ToolError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Tool '{name}' is turned off.")


class ToolExecutionDisabledError(ToolError):
    def __init__(self) -> None:
        super().__init__("Tool execution is turned off.")


class ToolPermissionError(ToolError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Tool '{name}' is not allowed for this agent.")


class ToolApprovalRequired(ToolError):
    """The mode requires approval. This phase does not run the tool or ask the user."""

    def __init__(self, message: str = "This action needs approval.") -> None:
        super().__init__(message)


class ToolValidationError(ToolError):
    def __init__(self, message: str = "The tool arguments are not valid.") -> None:
        super().__init__(message)


class ToolExecutionError(ToolError):
    """A tool failed in a way that is safe to describe."""

    def __init__(self, message: str = "The tool could not be completed.") -> None:
        super().__init__(message)


class SearchProviderError(ToolError):
    """The external search provider did not return a usable response."""

    def __init__(self, *, rate_limited: bool = False, timed_out: bool = False) -> None:
        super().__init__("Web search did not return results.")
        self.rate_limited = rate_limited
        self.timed_out = timed_out
