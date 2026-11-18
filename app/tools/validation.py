from app.tools.exceptions import ToolValidationError


def validate_arguments(schema: dict, arguments: object) -> dict:
    """Check a tool call against its declared object schema."""
    if not isinstance(arguments, dict):
        raise ToolValidationError("Tool arguments must be an object.")
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    if schema.get("additionalProperties") is False:
        unexpected = [key for key in arguments if key not in properties]
        if unexpected:
            raise ToolValidationError("Unexpected tool arguments.")
    for name in schema.get("required") or []:
        if name not in arguments:
            raise ToolValidationError(f"Missing tool argument '{name}'.")
    cleaned: dict = {}
    for name, spec in properties.items():
        if name not in arguments:
            continue
        cleaned[name] = _check(name, spec if isinstance(spec, dict) else {}, arguments[name])
    return cleaned


def _check(name: str, spec: dict, value: object) -> object:
    expected = spec.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise ToolValidationError(f"Tool argument '{name}' must be a string.")
        minimum = spec.get("minLength")
        maximum = spec.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ToolValidationError(f"Tool argument '{name}' is too short.")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ToolValidationError(f"Tool argument '{name}' is too long.")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolValidationError(f"Tool argument '{name}' must be an integer.")
        _check_range(name, spec, value)
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolValidationError(f"Tool argument '{name}' must be a number.")
        _check_range(name, spec, value)
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolValidationError(f"Tool argument '{name}' must be a boolean.")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolValidationError(f"Tool argument '{name}' is not an allowed value.")
    return value


def _check_range(name: str, spec: dict, value: int | float) -> None:
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        raise ToolValidationError(f"Tool argument '{name}' is too small.")
    if isinstance(maximum, (int, float)) and value > maximum:
        raise ToolValidationError(f"Tool argument '{name}' is too large.")
