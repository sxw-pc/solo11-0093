"""Typed errors with stable CLI exit codes and JSON-safe serialization."""

from __future__ import annotations


class LakeCommitError(Exception):
    exit_code = 1
    default_code = "INTERNAL_ERROR"

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        location: str = "",
        field_path: str = "",
        details: dict | None = None,
        exit_code: int | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.message = message or self.code
        self.location = location
        self.field_path = field_path
        self.details = details or {}
        if exit_code is not None:
            self.exit_code = exit_code
        super().__init__(self.message)

    def as_dict(self) -> dict:
        error: dict = {"code": self.code, "message": self.message}
        if self.location:
            error["location"] = self.location
        if self.field_path:
            error["field_path"] = self.field_path
        if self.details:
            error["details"] = self.details
        return {"error": error}


class InputError(LakeCommitError):
    exit_code = 2
    default_code = "INPUT_ERROR"


class SchemaError(LakeCommitError):
    exit_code = 3
    default_code = "VALIDATION_ERROR"


class StateError(LakeCommitError):
    exit_code = 3
    default_code = "STATE_ERROR"


class GoldenMismatchError(LakeCommitError):
    exit_code = 4
    default_code = "GOLDEN_MISMATCH"


class NetworkBlockedError(RuntimeError):
    """Raised by the local offline guard when an outbound connection is attempted."""


def as_error_payload(exc: LakeCommitError) -> dict:
    return exc.as_dict()
