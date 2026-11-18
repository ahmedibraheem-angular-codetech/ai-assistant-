"""Bind an approval to the exact operation the user reviewed.

Stored records keep digests, not raw secrets or complete file contents.
Different secret values stay different because the digest is of the real value.
The fingerprint is an HMAC, so a copied hash cannot be rebuilt without the
local key.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path

_SECRET_KEYS = ("password", "token", "secret", "credential", "api_key", "authorization", "access_token")
_PREVIEW_LIMIT = 400


def bind_arguments(arguments: dict) -> dict:
    """Canonical arguments for storage and fingerprints.

    Calling this twice returns the same object. Raw secrets and long text are
    replaced by their SHA-256 digests.
    """
    bound: dict = {}
    for key, value in arguments.items():
        if _secret(key):
            bound[key] = _digest_node(value)
        elif key == "content":
            bound[key] = _content_node(value)
        elif isinstance(value, str) and len(value) > _PREVIEW_LIMIT:
            bound[key] = _content_node(value)
        elif isinstance(value, str):
            bound[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            bound[key] = value
        elif isinstance(value, dict):
            bound[key] = bind_arguments(value)
        else:
            bound[key] = json.loads(json.dumps(value, default=str))
    return bound


_PUBLIC_HIDDEN = (
    "password",
    "passwd",
    "token",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "private_key",
    "privatekey",
    "bearer",
)
_ENVIRONMENT_KEYS = frozenset({"environment", "env", "env_values"})
_HIDDEN_REASON = "Hidden fields remain part of the integrity seal."


def present_arguments(arguments: dict) -> dict:
    """Arguments safe to show. Secret digests are masked. Content digests stay."""
    shown, _hidden = redacted_arguments(arguments)
    return shown


def redacted_arguments(arguments: dict) -> tuple[dict, list[dict]]:
    """Public form of one stored argument object. The stored record is not modified."""
    hidden: list[dict] = []
    if not isinstance(arguments, dict):
        return {}, hidden
    return _redact_mapping(arguments, hidden), hidden


def _redact_mapping(value: dict, hidden: list[dict]) -> dict:
    shown: dict = {}
    for key in sorted(value):
        if key in _ENVIRONMENT_KEYS or _public_secret(key):
            shown[key] = "***"
            hidden.append({"field": str(key), "reason": _HIDDEN_REASON})
            continue
        item = value[key]
        if isinstance(item, dict):
            shown[key] = _redact_mapping(item, hidden)
        elif isinstance(item, list):
            shown[key] = [_redact_mapping(entry, hidden) if isinstance(entry, dict) else entry for entry in item]
        else:
            shown[key] = item
    return shown


def _public_secret(key: object) -> bool:
    text = str(key).casefold()
    return any(part in text for part in _PUBLIC_HIDDEN)


def mask_arguments(arguments: dict) -> dict:
    """Stored form of an operation. Kept for existing callers."""
    return bind_arguments(arguments)


def operation_fingerprint(
    *,
    agent_id: str | None,
    tool_name: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    arguments: dict,
) -> str:
    body = {
        "action": action,
        "agent_id": agent_id or "",
        "arguments": bind_arguments(arguments),
        "resource_id": resource_id or "",
        "resource_type": resource_type,
        "tool_name": tool_name,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hmac.new(_fingerprint_key(), encoded, hashlib.sha256).hexdigest()


def content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(key: object) -> bool:
    text = str(key).casefold()
    return any(part in text for part in _SECRET_KEYS)


def _digest_node(value: object) -> dict:
    if isinstance(value, dict) and set(value) == {"digest"} and isinstance(value.get("digest"), str):
        return {"digest": value["digest"]}
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return {"digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def _content_node(value: object) -> dict:
    if (
        isinstance(value, dict)
        and "digest" in value
        and isinstance(value.get("digest"), str)
        and "characters" in value
    ):
        return {"digest": value["digest"], "characters": value["characters"]}
    if not isinstance(value, str):
        payload = json.dumps(value, sort_keys=True, default=str)
        return {"digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(), "characters": len(payload)}
    return {"digest": content_digest(value), "characters": len(value)}


def _fingerprint_key() -> bytes:
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return path.read_bytes()
    try:
        os.write(descriptor, os.urandom(32))
    finally:
        os.close(descriptor)
    return path.read_bytes()


def _key_path() -> Path:
    from app.core.config import get_settings

    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        database = Path(url.removeprefix("sqlite:///"))
        if database.name:
            return database.parent / "approval.key"
    return Path.home() / ".personal-ai-assistant" / "approval.key"
