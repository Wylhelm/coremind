"""Tests for reasoning prompt template rendering."""

from __future__ import annotations

import pytest

from coremind.errors import ReasoningError
from coremind.reasoning.prompts import list_templates, render_prompt


def test_list_templates_includes_v1() -> None:
    ids = list_templates()
    assert "reasoning.heavy.system.v1" in ids
    assert "reasoning.heavy.user.v1" in ids


def test_render_system_heavy() -> None:
    out = render_prompt("reasoning.heavy.system.v1")
    assert "reasoning layer" in out.lower()
    assert "json" in out.lower()


def test_render_user_template_injects_context() -> None:
    out = render_prompt(
        "reasoning.heavy.user.v1",
        snapshot_json='{"x": 1}',
        memory_excerpt="- something relevant",
        schema_json="{}",
    )
    assert '"x": 1' in out
    assert "something relevant" in out


def test_render_user_template_without_memory() -> None:
    out = render_prompt(
        "reasoning.heavy.user.v1",
        snapshot_json="{}",
        memory_excerpt="",
        schema_json="{}",
    )
    assert "no relevant memories" in out


def test_render_unknown_template_raises() -> None:
    with pytest.raises(ReasoningError):
        render_prompt("reasoning.bogus.v99")


def test_render_missing_variable_raises() -> None:
    with pytest.raises(ReasoningError):
        render_prompt("reasoning.heavy.user.v1")  # missing vars → StrictUndefined
