"""Errors the research tasks raise to the task queue."""

from __future__ import annotations


class ModelTransportError(RuntimeError):
    """The model call failed in transport (connection, timeout, provider error): there is no
    reply to validate and nothing to quarantine. Raised to the task queue so its retry budget
    applies (D-UI-167); distinct from a validated empty result and from a schema quarantine."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def raise_if_transport_failure(result: object) -> None:
    """Raise `ModelTransportError` for a guard result that is neither ok nor quarantined."""
    if getattr(result, "ok", False) or getattr(result, "quarantined", False):
        return
    out_of_credit = bool(getattr(result, "out_of_credit", False))
    raise ModelTransportError(
        str(getattr(result, "error", None) or "The model could not be reached."),
        retryable=not out_of_credit,
    )
