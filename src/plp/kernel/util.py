"""Small shared helpers: timestamps, JSON extraction, minimal schema validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    """Canonical UTC timestamp for the store: ISO-8601, millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ts(value: str) -> datetime:
    """Parse a stored ISO timestamp into an aware datetime (assumes UTC if naive)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def extract_json_object(text: str) -> str:
    """Extract the first balanced top-level {...} block from free text.

    Models (especially smaller ones) often wrap JSON in prose or code fences;
    this keeps structured output parsing tolerant.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in text")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced JSON object in text")


def validate_against_schema(value: Any, schema: dict) -> list[str]:
    """Minimal JSON-Schema validation: type, enum, required, nested object/array.

    Deliberately small (the LLM-facing surface uses the same checks); returns a
    list of human-readable errors (empty = valid).
    """
    errors: list[str] = []
    if value is None:
        return errors  # None always passes; "required" is handled at call sites
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"expected one of {schema['enum']}, got {value!r}")
        return errors
    t = schema.get("type")
    if t:
        if t == "string" and not isinstance(value, str):
            errors.append(f"expected string, got {type(value).__name__}")
            return errors
        if t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append(f"expected integer, got {type(value).__name__}")
            return errors
        if t == "number" and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            errors.append(f"expected number, got {type(value).__name__}")
            return errors
        if t == "boolean" and not isinstance(value, bool):
            errors.append(f"expected boolean, got {type(value).__name__}")
            return errors
        if t == "array" and not isinstance(value, list):
            errors.append(f"expected array, got {type(value).__name__}")
            return errors
        if t == "object" and not isinstance(value, dict):
            errors.append(f"expected object, got {type(value).__name__}")
            return errors
    if t == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, v in enumerate(value):
                for e in validate_against_schema(v, item_schema):
                    errors.append(f"[{i}] {e}")
    if t == "object" and isinstance(value, dict):
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                for e in validate_against_schema(value[key], sub):
                    errors.append(f"{key}: {e}")
    return errors
