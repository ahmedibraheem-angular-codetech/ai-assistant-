class ConversationError(Exception):
    """Base error for conversation operations."""


class ConversationNotFoundError(ConversationError):
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Conversation '{conversation_id}' was not found.")


class InvalidConversationIdError(ConversationError):
    def __init__(self) -> None:
        super().__init__("Conversation id must be a UUID.")


class AgentConversationConflictError(ConversationError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class ConversationArchivedError(ConversationError):
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__("This conversation is archived and cannot accept messages.")


class ModelNotConfiguredError(ConversationError):
    def __init__(self) -> None:
        super().__init__(
            "OLLAMA_MODEL is not set. Add an installed chat model name to your .env file."
        )


class ConversationPersistenceError(ConversationError):
    def __init__(self) -> None:
        super().__init__("The conversation could not be saved.")
