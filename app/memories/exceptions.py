class MemoryError(Exception):
    """Base error for explicit personal memories."""


class MemoryNotFoundError(MemoryError):
    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id
        super().__init__(f"Memory '{memory_id}' was not found.")


class InvalidMemoryIdError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory id must be a UUID.")


class DuplicateMemoryKeyError(MemoryError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"A memory with the key '{key}' already exists.")


class MemoryPersistenceError(MemoryError):
    def __init__(self) -> None:
        super().__init__("The memory could not be saved.")


class MemoryActionNotFoundError(MemoryError):
    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(f"Memory action '{action_id}' was not found.")


class InvalidMemoryActionIdError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory action id must be a UUID.")


class MemoryActionStateError(MemoryError):
    """The action expired, was already used, or no longer matches its target."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
