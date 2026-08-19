"""Credential-only redaction for payloads emitted by ordinary runtime diagnostics.

Image data, prompts, and request content are intentionally preserved.  Callers
must pass structured headers and bodies whenever possible so credential-bearing
fields can be removed before serialization.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "[REDACTED]"

_CREDENTIAL_NAME_PATTERN = (
    r"(?:authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|"
    r"(?:[a-z0-9]+[-_])*(?:api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"id[-_]?token|session[-_]?token|auth[-_]?token|token|password|passwd|"
    r"secret|signature))"
)
_CREDENTIAL_KEY = re.compile(
    rf"^{_CREDENTIAL_NAME_PATTERN}$",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*:\s*)"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_JSON_CREDENTIAL_FIELD = re.compile(
    rf'(?i)("{_CREDENTIAL_NAME_PATTERN}"\s*:\s*)'
    r'("(?:\\.|[^"\\])*"|[^,}\]\s]+)'
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?i)\b({_CREDENTIAL_NAME_PATTERN})(\s*[=:]\s*)([^\s&#,;]+)"
)
_CREDENTIAL_QUERY = re.compile(
    rf"(?i)([?&]{_CREDENTIAL_NAME_PATTERN}=)([^&#\s]+)"
)


def redact_credentials(value: Any) -> Any:
    """Return an output-safe copy while preserving non-credential payload data.

    Mapping keys with credential semantics are replaced wholesale.  Other
    values are traversed recursively, and credential patterns embedded in text
    are redacted without truncating prompts, Base64, or data URLs.
    """
    return _redact(value, active=set())


def _redact(value: Any, *, active: set[int]) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        try:
            return _redact_text(value.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, Mapping):
        return _redact_mapping(value, active=active)
    if isinstance(value, list):
        return _redact_sequence(value, active=active, factory=list)
    if isinstance(value, tuple):
        return _redact_sequence(value, active=active, factory=tuple)
    return value


def _redact_mapping(value: Mapping[Any, Any], *, active: set[int]) -> dict[Any, Any]:
    identity = id(value)
    if identity in active:
        raise ValueError("diagnostic payload contains a cycle")
    active.add(identity)
    try:
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _CREDENTIAL_KEY.fullmatch(key.strip())
                else _redact(item, active=active)
            )
            for key, item in value.items()
        }
    finally:
        active.remove(identity)


def _redact_sequence(value: Any, *, active: set[int], factory: Any) -> Any:
    identity = id(value)
    if identity in active:
        raise ValueError("diagnostic payload contains a cycle")
    active.add(identity)
    try:
        return factory(_redact(item, active=active) for item in value)
    finally:
        active.remove(identity)


def _redact_text(value: str) -> str:
    value = _JSON_CREDENTIAL_FIELD.sub(
        lambda match: f'{match.group(1)}"{REDACTED}"', value,
    )
    value = _AUTHORIZATION_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value,
    )
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _CREDENTIAL_QUERY.sub(lambda match: f"{match.group(1)}{REDACTED}", value)
    return _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value,
    )
