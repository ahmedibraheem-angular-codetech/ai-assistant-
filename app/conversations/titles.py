"""Local, replaceable conversation titles.

No model call. A later title generator can replace this function without
changing conversation storage.
"""

DEFAULT_TITLE = "New conversation"
MAX_TITLE_LENGTH = 80


def generate_conversation_title(message: str, max_length: int = MAX_TITLE_LENGTH) -> str:
    collapsed = " ".join(message.split())
    if not collapsed:
        return DEFAULT_TITLE
    if len(collapsed) <= max_length:
        return collapsed
    trimmed = collapsed[: max_length - 1].rstrip()
    return f"{trimmed}…"
