"""Intention prompt rendering."""

from __future__ import annotations

import pytest

from coremind.errors import IntentionError
from coremind.intention.prompts import render_prompt


def test_system_renders() -> None:
    out = render_prompt("intention.system.v1")
    assert "intention layer" in out.lower()


def test_user_renders_with_context() -> None:
    out = render_prompt(
        "intention.user.v1",
        snapshot_json="{}",
        reasoning_summary="",
        recent_intents_summary="",
        patterns_summary="",
        schema_json="{}",
        max_questions=5,
    )
    assert "World snapshot" in out
    assert "max_questions" not in out  # the variable is rendered, not the name


def test_unknown_template_raises() -> None:
    with pytest.raises(IntentionError):
        render_prompt("intention.nope.v1")
