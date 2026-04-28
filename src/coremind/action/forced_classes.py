"""Forced-approval action classes.

The classes listed here are *hardcoded* to require explicit user approval
regardless of the reasoning layer's confidence or a plugin's self-reported
category.  This list is NOT overridable by plugin manifests.

If a plugin or the intention layer returns ``safe`` or ``suggest`` for an
action whose class falls into one of these families, the router forces the
category to ``ask`` and emits a ``security.category.override_blocked``
meta-event so the operator can audit the attempt.

See `ARCHITECTURE.md §15.4` and `docs/phases/PHASE_3_INTENTION_ACTION.md §3.4`.
"""

from __future__ import annotations

from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Hardcoded families — every action class matching one of these prefixes
# (or equal to the literal) is forced into ``ask``.
# ---------------------------------------------------------------------------

_FORCED_PREFIXES: tuple[str, ...] = (
    # Financial — any movement of money, investment, or purchase.
    "finance.",
    "payment.",
    "banking.",
    "billing.",
    # Outbound messaging — any third-party contact surface.
    "email.outbound",
    "sms.outbound",
    "chat.outbound",
    "social.outbound",
    "message.outbound",
    # Credentials & infrastructure.
    "credentials.",
    "secrets.",
    "infrastructure.",
    "apikey.",
    # Plugin lifecycle / permissions.
    "plugin.install",
    "plugin.grant",
    "plugin.permission",
    # Safety-mechanism modifications.
    "coremind.safety",
    "coremind.forced_class",
    "coremind.signing_key",
)


def is_forced_ask(action_class: str, *, user_ask_classes: Iterable[str] = ()) -> bool:
    """Return ``True`` when ``action_class`` must be routed through ``ask``.

    Matching rules:

    - Exact-equal match against any entry in ``_FORCED_PREFIXES`` (case
      sensitive).
    - Prefix match against any entry that ends with ``"."``.
    - Exact-equal match against any user-configured class in
      ``user_ask_classes``.

    Args:
        action_class: The class string declared on the proposed action.
        user_ask_classes: Additional classes the user has listed in
            ``config.ask_classes``.  Exact-match only.

    Returns:
        ``True`` if the class is forced, ``False`` otherwise.
    """
    if not action_class:
        return False
    if action_class in user_ask_classes:
        return True
    for entry in _FORCED_PREFIXES:
        if entry.endswith("."):
            if action_class.startswith(entry):
                return True
        elif action_class == entry or action_class.startswith(entry + "."):
            return True
    return False
