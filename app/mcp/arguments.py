"""Validate one MCP tool argument value and its declared schema.

The canonical JSON produced here is the value that is encrypted and later
written. Whitespace is not preserved. Unsupported schema keywords fail closed.
"""

from __future__ import annotations

import hashlib
import json
import math

_MAX_CANONICAL = 32 * 1024
_MAX_DEPTH = 8
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
    }
)
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_BANNED = frozenset(
    {
        "$ref",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "dependencies",
        "unevaluatedProperties",
    }
)


class ArgumentError(Exception):
    def __init__(self) -> None:
        super().__init__("The MCP arguments are not valid.")


def canonical_json(value: object) -> str:
    """Return the exact JSON that is hashed, encrypted, and dispatched."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ArgumentError() from exc
    if len(text.encode("utf-8")) > _MAX_CANONICAL:
        raise ArgumentError()
    try:
        parsed = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ArgumentError() from exc
    if _shape(parsed) != _shape(value):
        raise ArgumentError()
    return text


def canonical_object(value: object) -> tuple[str, object]:
    text = canonical_json(value)
    parsed = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    return text, parsed


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_schema(schema: object) -> str:
    """Accept one schema and return its canonical JSON."""
    _schema(schema, 1)
    return canonical_json(schema)


def validate_mcp_arguments(schema: object, value: object) -> tuple[str, object]:
    """Return canonical JSON and the ordered value that will be dispatched."""
    _schema(schema, 1)
    text, parsed = canonical_object(value)
    _instance(schema, parsed, 1)
    return text, parsed


def _pairs(items: list[tuple[str, object]]) -> dict:
    found: dict = {}
    for key, value in items:
        if key in found:
            raise ValueError("duplicate")
        found[key] = value
    return found


def _reject_constant(_value: str) -> None:
    raise ValueError("constant")


def _shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ArgumentError()
    return type(value)


def _schema(schema: object, depth: int) -> None:
    if depth > _MAX_DEPTH or not isinstance(schema, dict):
        raise ArgumentError()
    keys = set(schema)
    if keys & _BANNED or not keys <= _SCHEMA_KEYS or "type" not in schema:
        raise ArgumentError()
    kind = schema["type"]
    if not isinstance(kind, str) or kind not in _TYPES:
        raise ArgumentError()
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not choices or len(choices) > 32:
            raise ArgumentError()
    if kind == "object":
        if schema.get("additionalProperties") is not False:
            raise ArgumentError()
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or len(properties) > 32 or not isinstance(required, list):
            raise ArgumentError()
        for name, child in properties.items():
            if not isinstance(name, str) or not name or len(name) > 64 or "\x00" in name:
                raise ArgumentError()
            _schema(child, depth + 1)
        for name in required:
            if not isinstance(name, str) or name not in properties:
                raise ArgumentError()
        return
    if kind == "array":
        if "items" not in schema:
            raise ArgumentError()
        maximum = schema.get("maxItems", 32)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0 or maximum > 32:
            raise ArgumentError()
        _schema(schema["items"], depth + 1)
        return
    if kind == "string":
        _bounds(schema, "minLength", "maxLength", 400)
        return
    if kind in {"integer", "number"}:
        _number_bound(schema, "minimum")
        _number_bound(schema, "maximum")
        return
    if any(key in schema for key in ("properties", "required", "items", "minLength", "maxLength", "minimum", "maximum")):
        raise ArgumentError()


def _bounds(schema: dict, low: str, high: str, default_high: int) -> None:
    minimum = schema.get(low, 0)
    maximum = schema.get(high, default_high)
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 0
        or maximum < minimum
        or maximum > 400
    ):
        raise ArgumentError()


def _number_bound(schema: dict, name: str) -> None:
    if name not in schema:
        return
    value = schema[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ArgumentError()


def _instance(schema: dict, value: object, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ArgumentError()
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ArgumentError()
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in properties for key in value) or any(name not in value for name in required):
            raise ArgumentError()
        for name, item in value.items():
            _instance(properties[name], item, depth + 1)
    elif kind == "array":
        if not isinstance(value, list):
            raise ArgumentError()
        maximum = schema.get("maxItems", 32)
        if len(value) > maximum:
            raise ArgumentError()
        for item in value:
            _instance(schema["items"], item, depth + 1)
    elif kind == "string":
        if not isinstance(value, str) or "\x00" in value:
            raise ArgumentError()
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 400):
            raise ArgumentError()
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ArgumentError()
        _range(schema, value)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
            raise ArgumentError()
        _range(schema, value)
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ArgumentError()
    elif kind == "null":
        if value is not None:
            raise ArgumentError()
    else:
        raise ArgumentError()
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError()


def _range(schema: dict, value: int | float) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise ArgumentError()
    if "maximum" in schema and value > schema["maximum"]:
        raise ArgumentError()


def _finite(value: int | float) -> bool:
    return isinstance(value, int) or math.isfinite(value)
