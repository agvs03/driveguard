"""Domain errors with stable codes. MCP servers turn these into the error envelope
`{"code", "message", "retryable"}`; messages must never contain secrets,
stack traces or raw untrusted input."""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"  # optimistic-lock version mismatch
    FAILED_PRECONDITION = "FAILED_PRECONDITION"  # e.g. illegal case state transition
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"


RETRYABLE = {Code.RATE_LIMITED, Code.CONFLICT, Code.INTERNAL}


class DriveGuardError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def envelope(self) -> dict[str, object]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


def invalid(msg: str) -> DriveGuardError:
    return DriveGuardError(Code.INVALID_ARGUMENT, msg)


def not_found(msg: str) -> DriveGuardError:
    return DriveGuardError(Code.NOT_FOUND, msg)


def forbidden(msg: str) -> DriveGuardError:
    return DriveGuardError(Code.FORBIDDEN, msg)
