"""Arithmetic with a small parser. Python eval and exec are not used."""

import math

from app.tools.base import BLOCKING_READ_ONLY, Tool
from app.tools.exceptions import ToolValidationError

_INVALID = "That expression is not supported. Use numbers and + - * / ( )."
_DIVISION = "Division by zero is not allowed."


def evaluate(expression: str) -> int | float:
    if not isinstance(expression, str) or len(expression) > 200:
        raise ToolValidationError(_INVALID)
    return _Parser(_tokenize(expression)).parse()


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(expression)
    while index < length:
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            index += 1
            continue
        if char.isdigit() or char == ".":
            start = index
            saw_digit = False
            saw_dot = False
            while index < length and (expression[index].isdigit() or expression[index] == "."):
                if expression[index] == ".":
                    if saw_dot:
                        raise ToolValidationError(_INVALID)
                    saw_dot = True
                else:
                    saw_digit = True
                index += 1
            if not saw_digit:
                raise ToolValidationError(_INVALID)
            tokens.append(expression[start:index])
            continue
        raise ToolValidationError(_INVALID)
    if not tokens or len(tokens) > 64:
        raise ToolValidationError(_INVALID)
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> int | float:
        value = self._expression()
        if self._peek() is not None:
            raise ToolValidationError(_INVALID)
        return _finite(value)

    def _expression(self) -> float:
        value = self._term()
        while self._peek() in {"+", "-"}:
            operator = self._pop()
            right = self._term()
            value = value + right if operator == "+" else value - right
        return value

    def _term(self) -> float:
        value = self._factor()
        while self._peek() in {"*", "/"}:
            operator = self._pop()
            right = self._factor()
            if operator == "/":
                if right == 0:
                    raise ToolValidationError(_DIVISION)
                value = value / right
            else:
                value = value * right
        return value

    def _factor(self) -> float:
        token = self._peek()
        if token == "-":
            self._pop()
            return -self._factor()
        if token == "(":
            self._pop()
            value = self._expression()
            if self._pop() != ")":
                raise ToolValidationError(_INVALID)
            return value
        if token is None or token in {"+", "*", "/", ")"}:
            raise ToolValidationError(_INVALID)
        self._pop()
        return float(token)

    def _peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _pop(self) -> str:
        token = self._peek()
        if token is None:
            raise ToolValidationError(_INVALID)
        self.index += 1
        return token


def _finite(value: float) -> int | float:
    if not math.isfinite(value):
        raise ToolValidationError(_INVALID)
    rounded = round(value)
    if abs(value - rounded) < 1e-9 and abs(rounded) < 10**12:
        return int(rounded)
    return round(value, 10)


class CalculatorTool(Tool):
    name = "calculator"
    description = "Evaluate arithmetic with numbers, parentheses, and + - * /."
    category = "utility"
    requires_confirmation = False
    execution_policy = BLOCKING_READ_ONLY
    isolation_key = "calculator"
    timeout_seconds = 2.0
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["expression"],
        "properties": {
            "expression": {"type": "string", "minLength": 1, "maxLength": 200},
        },
    }

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        value = evaluate(str(arguments["expression"]))
        return {"expression": arguments["expression"], "value": value}
