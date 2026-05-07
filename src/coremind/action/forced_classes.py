"""Forced-approval action classes.

The classes listed here are *hardcoded* to require explicit user approval
regardless of the reasoning layer's confidence or a plugin's self-reported
category.  This list is NOT overridable by plugin manifests.

If a plugin or the intention layer returns ``safe`` or ``suggest`` for an
action whose class falls into one of these families, the router forces the
category to ``ask`` and emits a ``security.category.override_blocked``
meta-event so the operator can audit the attempt.

See `ARCHITECTURE.md §15.4` and `docs/phases/PHASE_3_INTENTION_ACTION.md §3.4`.

.. note::

   This module delegates to :func:`coremind.action.action_classes.get_forced_category`
   which also handles SAFE and SUGGEST category overrides.  Kept for
   backward compatibility.
"""

from __future__ import annotations

from collections.abc import Iterable

from coremind.action.action_classes import get_forced_category


def is_forced_ask(action_class: str, *, user_ask_classes: Iterable[str] = ()) -> bool:
    """Return ``True`` when ``action_class`` must be routed through ``ask``.

    Delegates to :func:`~coremind.action.action_classes.get_forced_category`.
    Any class that resolves to ``"ask"`` is considered forced.

    Args:
        action_class: The class string declared on the proposed action.
        user_ask_classes: Additional classes the user has listed in
            ``config.ask_classes``.  Exact-match only.

    Returns:
        ``True`` if the class is forced, ``False`` otherwise.
    """
    return get_forced_category(action_class, user_ask_classes=tuple(user_ask_classes)) == "ask"
