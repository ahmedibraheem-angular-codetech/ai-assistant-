from app.tools.exceptions import ToolExecutionError


class WorkspaceError(Exception):
    """Base error for workspace registration. Messages do not include file contents."""


class WorkspaceNotFoundError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__("That workspace was not found.")


class DuplicateWorkspaceNameError(WorkspaceError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"A workspace named '{name}' already exists.")


class DuplicateWorkspaceRootError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__("That directory is already a workspace.")


class WorkspaceApprovalError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__(
            "Read-only access must be confirmed. The directory is not registered."
        )


class WorkspacePathError(WorkspaceError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class WorkspacePermissionError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__(
            "The operating system denied access. macOS privacy protections were not bypassed."
        )


class WorkspaceLimitError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__("The workspace limit has been reached.")


class InvalidWorkspaceIdError(WorkspaceError):
    def __init__(self) -> None:
        super().__init__("Workspace id must be a UUID.")


class FileAccessError(ToolExecutionError):
    """A file tool failed in a way that is safe to show. Paths are not included."""
