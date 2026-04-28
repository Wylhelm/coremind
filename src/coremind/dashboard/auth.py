"""Authentication and origin policy for the dashboard's write endpoint.

The dashboard is otherwise read-only, but ``/api/approvals`` lets the
operator forward an :class:`~coremind.notify.port.ApprovalResponse` through
the in-process :class:`~coremind.notify.adapters.dashboard.DashboardNotificationPort`.
That endpoint can promote forced-approval-class intents to execution, so
loopback binding alone is not a sufficient trust boundary on multi-user
hosts.

This module pins the requirements:

- A bearer token must accompany every approval submission.  The token is
  loaded from the daemon's secrets store and compared with constant-time
  equality.
- The request's ``Origin`` (or ``Referer`` when ``Origin`` is absent) must
  match the configured dashboard origin so a drive-by request from another
  localhost service cannot CSRF an approval.
- The journal entry's ``responder`` is taken from configuration, not from
  the request body, so the audit log attributes the approval to a real
  operator identity rather than a hardcoded ``"dashboard"`` literal.
"""

from __future__ import annotations

import hmac

from pydantic import BaseModel, ConfigDict, Field

from coremind.notify.port import UserRef


class DashboardAuth(BaseModel):
    """Auth policy for the dashboard's approval-submission endpoint.

    Attributes:
        api_token: Shared secret required as ``Authorization: Bearer <token>``.
            Stored in ``~/.coremind/secrets/`` by the daemon and injected at
            startup.  Constant-time compared on every request.
        operator: Identity recorded as the responder on every approval the
            dashboard forwards.  Comes from config so the audit journal can
            attribute *who* approved.
        allowed_origins: Origins permitted on inbound approval requests.
            Empty tuple means "no caller is allowed", which is the safe
            default when the dashboard is started without explicit policy.
    """

    model_config = ConfigDict(frozen=True)

    api_token: str = Field(min_length=16)
    operator: UserRef
    allowed_origins: tuple[str, ...] = ()

    def token_matches(self, candidate: str | None) -> bool:
        """Return ``True`` iff ``candidate`` equals :attr:`api_token`.

        Uses :func:`hmac.compare_digest` to avoid leaking the token via
        timing differences.  ``None`` and the empty string never match.
        """
        if not candidate:
            return False
        return hmac.compare_digest(self.api_token, candidate)

    def origin_allowed(self, origin: str | None) -> bool:
        """Return ``True`` iff ``origin`` is in :attr:`allowed_origins`.

        ``None`` (no header) is rejected because every browser submitting a
        ``fetch`` from the dashboard origin will set ``Origin`` automatically;
        a missing header indicates a non-browser caller and warrants a 403.
        """
        if not origin:
            return False
        return origin in self.allowed_origins


__all__ = ["DashboardAuth"]
