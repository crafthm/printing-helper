"""Error types with stable exit codes for printing-helper."""

from __future__ import annotations


class PhError(Exception):
    """Base error carrying an exit code and optional machine-readable payload."""

    code = 1

    def __init__(self, message: str, *, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "code": self.code, **self.payload}


class UsageError(PhError):
    code = 2


class InputError(PhError):
    code = 3


class PrinterError(PhError):
    code = 4


class ComposeError(PhError):
    code = 5


class SendError(PhError):
    code = 6


class NeedsConfirmation(PhError):
    code = 7

    def __init__(self, message: str, *, payload: dict | None = None):
        super().__init__(message, payload=payload)