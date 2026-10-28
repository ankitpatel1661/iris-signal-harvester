"""Exception hierarchy for the harvester.

The split that matters operationally is between errors that justify a retry and
errors that must never be retried. ``AccessRestricted`` is deliberately terminal:
the brief requires that access restrictions are respected, so a 401/403/CAPTCHA
stops work on that source rather than triggering a retry loop that would look
like an attempt to get around it.
"""

from __future__ import annotations

__all__ = [
    "AccessRestricted",
    "ConfigurationError",
    "HarvesterError",
    "MalformedRecord",
    "RecordRejected",
    "SourceUnavailable",
    "UnexpectedResponse",
]


class HarvesterError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(HarvesterError):
    """Configuration is missing or internally inconsistent. Fail fast at startup."""


class SourceUnavailable(HarvesterError):
    """Transient source failure: connection error, timeout, 5xx, or 429.

    Retryable. Carries ``retry_after`` when the server supplied one so the
    caller can honour it exactly rather than guessing a backoff.
    """

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AccessRestricted(HarvesterError):
    """The source refuses access: 401, 403, a login wall, or a robots.txt disallow.

    Terminal by design. Never retried, never worked around. The run records this
    as a finding and moves on.
    """

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class MalformedRecord(HarvesterError):
    """A single record could not be parsed or validated. The page continues."""

    def __init__(self, message: str, *, reason_code: str, raw: object = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.raw = raw


class RecordRejected(HarvesterError):
    """A record failed a validation gate and must go to the dead-letter table.

    ``reason_code`` is a stable, countable identifier (see ``RejectCode``), not a
    free-text message, so the quality report can histogram it.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


class UnexpectedResponse(HarvesterError):
    """A non-retryable, non-access status (e.g. 404, 400). Terminal for this request."""

    def __init__(self, message: str, *, status: int, url: str) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
