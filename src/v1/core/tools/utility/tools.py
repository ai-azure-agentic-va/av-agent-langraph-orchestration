"""General-purpose utility tools for agent reasoning support.

Exposed to the ServiceNow subagent so it can answer time-relative ticket
questions ("how old is this ticket?") and do reliable arithmetic (counts,
durations, percentages) instead of computing in its head.
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime, timezone as dt_timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool
from pydantic import Field

SOURCE = "utility"

MAX_EXPRESSION_LENGTH = 500
_MAX_POW_EXPONENT = 1000

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}


class CalculatorError(ValueError):
    """Raised when an expression is invalid or uses unsupported syntax."""


def _error_payload(exc: Exception, *, kind: str = "invalid_input") -> dict[str, Any]:
    return {
        "ok": False,
        "source": SOURCE,
        "kind": kind,
        "error": str(exc),
    }


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculatorError("only numeric literals are supported")
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculatorError(f"unknown name '{node.id}'")

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))

    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise CalculatorError(f"exponent magnitude must be {_MAX_POW_EXPONENT} or less")
        return _BIN_OPS[type(node.op)](left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalculatorError("only these functions are supported: " + ", ".join(sorted(_FUNCTIONS)))
        if node.keywords:
            raise CalculatorError("keyword arguments are not supported")
        args = [_eval_node(arg) for arg in node.args]
        return _FUNCTIONS[node.func.id](*args)

    raise CalculatorError(f"unsupported syntax: {type(node).__name__}")


def evaluate_expression(expression: str) -> float:
    """Safely evaluate an arithmetic expression without using eval()."""

    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("expression must be a non-empty string")

    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError(f"expression must be {MAX_EXPRESSION_LENGTH} characters or less")

    try:
        parsed = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"invalid expression: {exc.msg}") from exc

    return _eval_node(parsed)


@tool
def get_current_datetime(
    timezone: Annotated[
        str | None,
        Field(
            description=(
                "Optional IANA timezone name, e.g. 'America/New_York' or "
                "'Europe/London'. Defaults to UTC."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Get the current date and time, in UTC or a requested timezone."""

    try:
        tzinfo = ZoneInfo(timezone) if timezone else dt_timezone.utc
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return _error_payload(
            CalculatorError(f"unknown timezone '{timezone}': {exc}"),
            kind="invalid_timezone",
        )

    now = datetime.now(tzinfo)
    utc_now = now.astimezone(dt_timezone.utc)

    # Zone label rendered alongside the time so the model never quotes a bare
    # clock value: 'UTC' for the default, otherwise the IANA name the caller asked
    # for. This puts the timezone right next to the time in the output (the
    # separate ``timezone`` field is easy to overlook when the model echoes the
    # value), e.g. ``'2026-06-26 14:30:00 UTC'``.
    zone_label = timezone or "UTC"

    return {
        "ok": True,
        "source": SOURCE,
        "kind": "current_datetime",
        "iso": now.isoformat(),
        "utc_iso": utc_now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        # Human-readable date+time WITH the timezone marker (UTC by default).
        "display": f"{now.strftime('%Y-%m-%d %H:%M:%S')} {zone_label}",
        "day_of_week": now.strftime("%A"),
        "timezone": zone_label,
        "unix_timestamp": int(utc_now.timestamp()),
    }


@tool
def calculator(
    expression: Annotated[
        str,
        Field(
            description=(
                "Arithmetic expression to evaluate, e.g. '(34 - 12) / 7' or "
                "'sqrt(2) * 10**3'. Supports + - * / // % **, parentheses, "
                "the constants pi and e, and the functions abs, round, min, "
                "max, sqrt, floor, ceil, log, log10, exp."
            )
        ),
    ],
) -> dict[str, Any]:
    """Evaluate an arithmetic expression and return the numeric result."""

    try:
        result = evaluate_expression(expression)
    except CalculatorError as exc:
        return _error_payload(exc)
    except (ZeroDivisionError, OverflowError, TypeError) as exc:
        return _error_payload(exc, kind="math_error")

    return {
        "ok": True,
        "source": SOURCE,
        "kind": "calculation",
        "expression": expression,
        "result": result,
    }


UTILITY_TOOLS = [
    get_current_datetime,
    calculator,
]


__all__ = [
    "UTILITY_TOOLS",
    "CalculatorError",
    "calculator",
    "evaluate_expression",
    "get_current_datetime",
]
