"""Read-only view of one sealed approval.

The view is derived from the stored record. It does not read MCP configuration,
spawn a server, or replace the stored fingerprint.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from app.approvals.fingerprint import operation_fingerprint, redacted_arguments

MCP_CALL_TOOL = "call_mcp_tool"
MCP_READ_TOOL = "read_mcp_resource"
MCP_TOOLS = frozenset({MCP_CALL_TOOL, MCP_READ_TOOL})

_CALL_FIELDS = (
    ("server_id", "MCP server identity"),
    ("server_display_name", "MCP server name"),
    ("config_revision", "Configuration revision"),
    ("epoch", "Configuration epoch"),
    ("executable_device", "Executable device"),
    ("executable_inode", "Executable inode"),
    ("executable_sha256", "Executable content"),
    ("env_sha256", "Environment"),
    ("remote_tool_name", "MCP tool name"),
    ("input_schema_sha256", "Tool schema"),
    ("arguments_sha256", "Arguments seal"),
    ("process_generation", "Process generation"),
    ("discovery_generation", "Discovery generation"),
    ("approval_id", "Approval identity"),
    ("expires_at", "Expiration"),
)
_READ_FIELDS = (
    ("server_id", "MCP server identity"),
    ("server_display_name", "MCP server name"),
    ("config_revision", "Configuration revision"),
    ("epoch", "Configuration epoch"),
    ("executable_device", "Executable device"),
    ("executable_inode", "Executable inode"),
    ("executable_sha256", "Executable content"),
    ("env_sha256", "Environment"),
    ("resource_uri", "Resource identifier"),
    ("process_generation", "Process generation"),
    ("discovery_generation", "Discovery generation"),
    ("approval_id", "Approval identity"),
    ("expires_at", "Expiration"),
)
_HASH_FIELDS = frozenset(
    {
        "config_revision",
        "executable_sha256",
        "env_sha256",
        "input_schema_sha256",
        "arguments_sha256",
    }
)
_TEXT_FIELDS = frozenset(
    {"server_id", "server_display_name", "remote_tool_name", "resource_uri", "approval_id", "expires_at"}
)
_INTEGER_FIELDS = frozenset({"epoch", "executable_device", "executable_inode"})
_GENERATION_FIELDS = frozenset({"process_generation", "discovery_generation"})


def build_review(record: Any) -> dict:
    """Return a deterministic review. The record is not modified."""
    parsed, parse_warning = _parse_arguments(getattr(record, "arguments_json", None))
    hmac_ok = parsed is not None and _fingerprint_matches(record, parsed)
    path_ok = parsed is not None and stored_path_matches(record, parsed)
    shown, hidden = redacted_arguments(parsed or {})
    warnings = []
    if parse_warning is not None:
        warnings.append(parse_warning)
    if parsed is not None and not hmac_ok:
        warnings.append("The stored operation does not match its fingerprint.")
    if parsed is not None and not path_ok:
        warnings.append("The authorization path does not match the sealed operation.")
    mcp_tool = getattr(record, "tool_name", "") in MCP_TOOLS
    fields, mcp_warnings, fields_ok = _mcp_fields(getattr(record, "tool_name", ""), parsed, record)
    warnings.extend(mcp_warnings)
    if mcp_tool:
        verified = hmac_ok and path_ok and fields_ok
    else:
        verified = hmac_ok and path_ok
    reviewable = parsed is not None and verified and not mcp_warnings
    if mcp_tool and reviewable:
        warnings.append("This MCP operation can be reviewed. Execution is not available.")
    return {
        "reviewable": reviewable,
        "verified": verified,
        "mcp_execution_available": False,
        "approval_id": getattr(record, "id", None),
        "status": getattr(record, "status", None),
        "operation": getattr(record, "action", None),
        "tool_name": getattr(record, "tool_name", None),
        "agent_id": getattr(record, "agent_id", None),
        "fingerprint": getattr(record, "fingerprint", None),
        "arguments": shown if parsed is not None else None,
        "arguments_text": _canonical(shown) if parsed is not None else None,
        "mcp": {
            "applies": mcp_tool,
            "live_verification": "not_performed" if mcp_tool else "not_applicable",
            "fields": fields,
        },
        "hidden": hidden,
        "warnings": warnings,
    }


def _parse_arguments(raw: object) -> tuple[dict | None, str | None]:
    if not isinstance(raw, str):
        return None, "The sealed arguments are unavailable."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "The sealed arguments cannot be read."
    if not isinstance(parsed, dict):
        return None, "The sealed arguments are not an operation."
    return parsed, None


def _fingerprint_matches(record: Any, arguments: dict) -> bool:
    stored = getattr(record, "fingerprint", None)
    if not isinstance(stored, str) or len(stored) != 64:
        return False
    current = operation_fingerprint(
        agent_id=getattr(record, "agent_id", None),
        tool_name=getattr(record, "tool_name", ""),
        action=getattr(record, "action", ""),
        resource_type=getattr(record, "resource_type", ""),
        resource_id=getattr(record, "resource_id", None),
        arguments=arguments,
    )
    return hmac.compare_digest(current, stored)


def stored_path_matches(record: Any, arguments: dict | None) -> bool:
    """True when the authorization column and the sealed arguments name the same path.

    A missing path on both sides matches. A non-string sealed path does not.
    """
    if not isinstance(arguments, dict):
        return False
    if "relative_path" not in arguments:
        sealed = None
    elif isinstance(arguments.get("relative_path"), str):
        sealed = arguments["relative_path"]
    else:
        return False
    column = getattr(record, "relative_path", None)
    column_path = column if isinstance(column, str) else None
    return sealed == column_path


def format_expiry(value: datetime) -> str:
    """UTC timestamp stored inside the sealed arguments."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _mcp_fields(tool_name: str, arguments: dict | None, record: Any = None) -> tuple[dict, list[str], bool]:
    if tool_name not in MCP_TOOLS:
        blank = {
            name: {"status": "not_applicable", "value": None}
            for name, _label in _CALL_FIELDS + (("resource_uri", "Resource identifier"),)
        }
        return blank, [], True
    required = _CALL_FIELDS if tool_name == MCP_CALL_TOOL else _READ_FIELDS
    fields: dict = {}
    warnings: list[str] = []
    complete = True
    body = arguments or {}
    for name, label in required:
        if name not in body:
            fields[name] = {"status": "unavailable", "value": None}
            warnings.append(f"The sealed record does not include {label}.")
            complete = False
            continue
        if not _valid_representation(name, body[name]):
            fields[name] = {"status": "invalid", "value": None}
            warnings.append(f"The sealed record has an invalid {label}.")
            complete = False
            continue
        fields[name] = {"status": "sealed", "value": _sealed_value(name, body[name])}
    if tool_name == MCP_CALL_TOOL:
        complete = _seal_call_arguments(body, fields, warnings) and complete
    if tool_name in MCP_TOOLS and record is not None:
        complete = _bound_to_row(record, body, fields, warnings) and complete
    return fields, warnings, complete


def _seal_call_arguments(body: dict, fields: dict, warnings: list[str]) -> bool:
    if "tool_arguments" not in body:
        fields["tool_arguments"] = {"status": "unavailable", "value": None}
        warnings.append("The sealed record does not include the tool arguments.")
        return False
    arguments = body["tool_arguments"]
    if not isinstance(arguments, dict):
        fields["tool_arguments"] = {"status": "invalid", "value": None}
        warnings.append("The sealed record has invalid tool arguments.")
        return False
    shown, _hidden = redacted_arguments({"tool_arguments": arguments})
    fields["tool_arguments"] = {"status": "sealed", "value": shown["tool_arguments"]}
    return True


def _bound_to_row(record: Any, body: dict, fields: dict, warnings: list[str]) -> bool:
    """The sealed identity and expiration must name this row."""
    complete = True
    if body.get("approval_id") != getattr(record, "id", None):
        fields["approval_id"] = {"status": "invalid", "value": None}
        warnings.append("The sealed record does not include the approval identity.")
        complete = False
    column = getattr(record, "expires_at", None)
    expires = body.get("expires_at")
    if not isinstance(column, datetime) or not isinstance(expires, str) or format_expiry(column) != expires:
        fields["expires_at"] = {"status": "invalid", "value": None}
        warnings.append("The sealed record does not include the expiration.")
        complete = False
    return complete


def _valid_representation(name: str, value: object) -> bool:
    if name in _HASH_FIELDS:
        return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
    if name in _GENERATION_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if name in _INTEGER_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if name == "approval_id":
        return isinstance(value, str) and len(value) == 36 and "\x00" not in value
    if name == "expires_at":
        return isinstance(value, str) and len(value) == 25 and value.endswith("+00:00") and "\x00" not in value
    if name in _TEXT_FIELDS:
        return isinstance(value, str) and bool(value.strip()) and "\x00" not in value and len(value) <= 400
    return False


def _arguments_digest(value: dict) -> str | None:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sealed_value(name: str, value: object) -> object:
    shown, _hidden = redacted_arguments({name: value})
    return shown[name]


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
