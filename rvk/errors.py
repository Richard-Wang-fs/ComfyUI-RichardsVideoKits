"""Stable error categories for the RVK storage core."""

from __future__ import annotations

from typing import Any


class RVKError(ValueError):
    """Base class for errors that are safe to surface to an RVK caller."""

    code = "RVK_ERROR"

    def __init__(
        self,
        message: str,
        *,
        path: str = "$",
        reason: str | None = None,
        actual: Any = None,
        expected: Any = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason
        self.actual = actual
        self.expected = expected

    def __str__(self) -> str:
        details = [f"{self.code} at {self.path}: {super().__str__()}"]
        if self.reason is not None:
            details.append(f"reason={self.reason}")
        if self.actual is not None:
            details.append(f"actual={self.actual!r}")
        if self.expected is not None:
            details.append(f"expected={self.expected!r}")
        return "; ".join(details)


class RecordInvalid(RVKError):
    """A job, segment, or canonical JSON value violates the v1 contract."""

    code = "RVK_RECORD_INVALID"


class AdapterUnsupported(RVKError):
    """No explicitly registered adapter contract matches the record."""

    code = "RVK_ADAPTER_UNSUPPORTED"


class JobConflict(RVKError):
    """A project root or fixed project identity conflicts with the request."""

    code = "RVK_JOB_CONFLICT"


class JobLocked(RVKError):
    """An OS writer lock is held or an in-flight worker has not stopped."""

    code = "RVK_JOB_LOCKED"


class AssetInvalid(RVKError):
    """An input or stored asset cannot be identified safely."""

    code = "RVK_ASSET_INVALID"


class CommitConflict(RVKError):
    """A formal segment target conflicts with the requested logical commit."""

    code = "RVK_COMMIT_CONFLICT"


class CommitUncertain(RVKError):
    """The segment crossed its publish point but final confirmation failed."""

    code = "RVK_COMMIT_UNCERTAIN"


class StorageFull(RVKError):
    """A storage operation failed because the target volume has no space."""

    code = "RVK_STORAGE_FULL"


class EncoderUnavailable(RVKError):
    """The pinned media backend cannot provide the resolved encoder stack."""

    code = "RVK_ENCODER_UNAVAILABLE"


class NoProgress(RVKError):
    """A media operation completed without producing a usable frame or packet."""

    code = "RVK_NO_PROGRESS"
