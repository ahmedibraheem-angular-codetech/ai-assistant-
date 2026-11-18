"""Local date and time in the configured timezone."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import get_settings
from app.tools.base import BLOCKING_READ_ONLY, Tool
from app.tools.exceptions import ToolExecutionError, ToolValidationError

_FORMAT = re.compile(r"^(?:%%|%[YmdHMSzZ]|[^%])+$")


class DateTimeTool(Tool):
    name = "datetime"
    description = (
        "Return the current date and time in the configured timezone, "
        "shift it by days, hours, or minutes, or format it. This uses the local clock."
    )
    category = "utility"
    requires_confirmation = False
    execution_policy = BLOCKING_READ_ONLY
    isolation_key = "datetime"
    timeout_seconds = 2.0
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["now", "add", "format"]},
            "days": {"type": "integer", "minimum": -3660, "maximum": 3660},
            "hours": {"type": "integer", "minimum": -24000, "maximum": 24000},
            "minutes": {"type": "integer", "minimum": -200000, "maximum": 200000},
            "format": {"type": "string", "minLength": 1, "maxLength": 80},
        },
    }

    def __init__(self) -> None:
        self.calls = 0
        self._now = datetime.now

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        zone = _configured_zone()
        moment = self._now(zone)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        operation = arguments["operation"]
        if operation == "add":
            moment = moment + timedelta(
                days=int(arguments.get("days", 0)),
                hours=int(arguments.get("hours", 0)),
                minutes=int(arguments.get("minutes", 0)),
            )
        rendered = moment.strftime("%Y-%m-%d %H:%M:%S %Z")
        if operation == "format":
            pattern = arguments.get("format")
            if not isinstance(pattern, str) or _FORMAT.fullmatch(pattern) is None:
                raise ToolValidationError("That date format is not supported.")
            rendered = moment.strftime(pattern)
        return {
            "timezone": getattr(moment.tzinfo, "key", None) or get_settings().default_timezone,
            "iso": moment.isoformat(timespec="seconds"),
            "date": moment.date().isoformat(),
            "time": moment.strftime("%H:%M:%S"),
            "formatted": rendered,
        }


def _configured_zone() -> ZoneInfo:
    name = get_settings().default_timezone.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ToolExecutionError("The configured timezone is not available.") from exc
