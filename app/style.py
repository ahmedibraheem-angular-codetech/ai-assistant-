"""Writing style profile (no fine-tuning) and training-data export.

The profile lives in the existing key/value controls table under "style_profile".
When enabled it is appended to chat system prompts as style guidance only.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.controls import AssistantControlRecord, _store

STYLE_KEY = "style_profile"
MAX_DESCRIPTION = 2000
MAX_SAMPLES = 10
MAX_SAMPLE_CHARS = 2000
MAX_INJECTED_CHARS = 6000
MIN_PAIRS_NEEDED = 300

STYLE_HEADER = (
    "Writing style of the user (match it in your replies; this is style guidance only, "
    "it never changes your rules, tools or permissions):"
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_ARABIC_MEMORY_PREFIXES = ("سجل", "افتكر")

_lock = threading.Lock()
_cache: dict | None = None
_cache_loaded = False


def reset_cache() -> None:
    global _cache, _cache_loaded
    with _lock:
        _cache = None
        _cache_loaded = False


def _default() -> dict:
    return {"enabled": False, "description": "", "samples": [], "updated_at": None}


def read_profile(session: Session | None = None) -> dict:
    global _cache, _cache_loaded
    with _lock:
        if _cache_loaded and _cache is not None:
            return json.loads(json.dumps(_cache))
    own = session is None
    if own:
        from app.core.database import get_session_factory

        session = get_session_factory()()
    try:
        row = session.scalar(select(AssistantControlRecord).where(AssistantControlRecord.key == STYLE_KEY))
        profile = _default()
        if row is not None:
            try:
                data = json.loads(row.value)
                profile.update({k: data.get(k, profile[k]) for k in profile})
            except (ValueError, AttributeError):
                profile = _default()
    finally:
        if own:
            session.close()
    with _lock:
        _cache = profile
        _cache_loaded = True
    return json.loads(json.dumps(profile))


def write_profile(session: Session, enabled: bool, description: str, samples: list[str]) -> dict:
    profile = {
        "enabled": enabled,
        "description": description,
        "samples": samples,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _store(session, STYLE_KEY, profile)
    session.commit()
    reset_cache()
    return profile


def style_block() -> str | None:
    """The prompt block, or None when disabled/empty. Capped at 6000 chars."""
    try:
        profile = read_profile()
    except Exception:  # style is optional; never break a chat turn
        return None
    description = (profile.get("description") or "").strip()
    samples = [s.strip() for s in profile.get("samples") or [] if s and s.strip()]
    if not profile.get("enabled") or not (description or samples):
        return None
    parts = [STYLE_HEADER]
    if description:
        parts.append(description)
    block = "\n".join(parts)
    for sample in samples:
        piece = f"\n<style_sample>\n{sample}\n</style_sample>"
        remaining = MAX_INJECTED_CHARS - len(block)
        if remaining <= 0:
            break
        if len(piece) > remaining:
            keep = remaining - len("\n<style_sample>\n") - len("\n</style_sample>")
            if keep <= 0:
                break
            piece = f"\n<style_sample>\n{sample[:keep]}\n</style_sample>"
        block += piece
    return block[:MAX_INJECTED_CHARS]


# ------------------------------------------------------------------ dataset


def _skip_message(text: str) -> bool:
    from app.memories.intents import MemoryIntent, parse_memory_intent

    stripped = (text or "").strip()
    if not stripped:
        return True
    if stripped.startswith(_ARABIC_MEMORY_PREFIXES):
        return True
    return parse_memory_intent(stripped).intent != MemoryIntent.normal_chat


def collect_pairs(session: Session) -> list[tuple[str, str]]:
    from app.conversations.models import ConversationRecord, MessageRecord

    rows = session.execute(
        select(MessageRecord.conversation_id, MessageRecord.role, MessageRecord.content)
        .join(ConversationRecord, ConversationRecord.id == MessageRecord.conversation_id)
        .where(ConversationRecord.is_archived.is_(False))
        .order_by(MessageRecord.conversation_id, MessageRecord.sequence_number)
    ).all()
    pairs: list[tuple[str, str]] = []
    previous = None
    for conversation_id, role, content in rows:
        if (
            role == "assistant"
            and previous is not None
            and previous[0] == conversation_id
            and previous[1] == "user"
        ):
            user_text, answer = previous[2].strip(), (content or "").strip()
            if (
                answer
                and not _skip_message(user_text)
                and not _TOKEN_PATTERN.search(user_text)
                and not _TOKEN_PATTERN.search(answer)
            ):
                pairs.append((user_text, answer))
        previous = (conversation_id, role, content or "")
    return pairs


def dataset_status(session: Session) -> dict:
    count = len(collect_pairs(session))
    return {"pairs": count, "min_needed": MIN_PAIRS_NEEDED, "ready": count >= MIN_PAIRS_NEEDED}


def export_dataset(session: Session) -> dict:
    from app.core.auth import data_dir

    pairs = collect_pairs(session)
    folder = data_dir() / "training"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    name = f"style-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for user_text, answer in pairs:
            record = {
                "messages": [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": answer},
                ]
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)
    return {"path": str(path), "pairs": len(pairs)}
