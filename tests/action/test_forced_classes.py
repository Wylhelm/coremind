"""Forced-approval class detection tests."""

from __future__ import annotations

from coremind.action.forced_classes import is_forced_ask


def test_exact_match() -> None:
    # entries without a trailing dot are matched both as exact and as prefix
    assert is_forced_ask("email.outbound")
    assert is_forced_ask("coremind.safety")
    assert is_forced_ask("plugin.install")


def test_prefix_match() -> None:
    assert is_forced_ask("finance.transfer")
    assert is_forced_ask("email.outbound.gmail")
    assert is_forced_ask("payment.refund")
    assert is_forced_ask("coremind.safety.pause")
    assert is_forced_ask("credentials.github")


def test_non_forced_class() -> None:
    assert not is_forced_ask("light")
    assert not is_forced_ask("hvac")
    assert not is_forced_ask("home.scene")
    # bare "finance" has no trailing dot in match list (``finance.``) and no
    # exact entry — so it is NOT forced.  Only ``finance.*`` is.
    assert not is_forced_ask("finance")


def test_user_declared_class() -> None:
    assert is_forced_ask("wearable", user_ask_classes=("wearable",))
    # user_ask_classes is exact-match only
    assert not is_forced_ask("wearable.heartrate", user_ask_classes=("wearable",))


def test_empty_class() -> None:
    assert not is_forced_ask("")
