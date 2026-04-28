"""Tests for the LLM wrapper (:mod:`coremind.reasoning.llm`)."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from coremind.errors import LLMError
from coremind.reasoning.llm import (
    LLM,
    CompletionResult,
    LayerConfig,
    LLMConfig,
    _approx_token_count,
    _strip_json_fences,
)


class _FakeResponse(BaseModel):
    name: str
    value: int


class _ScriptedBackend:
    """Backend that returns a predefined sequence of responses."""

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, str] | None,
        api_key: str | None,
    ) -> CompletionResult:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if not self._scripted:
            raise RuntimeError("backend out of scripted responses")
        content = self._scripted.pop(0)
        return CompletionResult(
            content=content,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )


def _config() -> LLMConfig:
    return LLMConfig(reasoning_heavy=LayerConfig(model="test/fake", max_completion_tokens=100))


def test_approx_token_count() -> None:
    assert _approx_token_count("") == 0
    assert _approx_token_count("abcd") == 1
    assert _approx_token_count("abcde") == 2


def test_strip_json_fences() -> None:
    assert _strip_json_fences("{}") == "{}"
    assert _strip_json_fences('```json\n{"x": 1}\n```') == '{"x": 1}'
    assert _strip_json_fences("```\n{}\n```") == "{}"


@pytest.mark.asyncio
async def test_complete_structured_happy_path() -> None:
    backend = _ScriptedBackend([json.dumps({"name": "a", "value": 1})])
    llm = LLM(_config(), backend=backend)
    result = await llm.complete_structured(
        layer="reasoning_heavy",
        system="sys",
        user="user",
        response_model=_FakeResponse,
    )
    assert result.name == "a" and result.value == 1
    assert len(backend.calls) == 1
    assert backend.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_retry_on_malformed_json() -> None:
    backend = _ScriptedBackend(
        [
            "not json at all",
            json.dumps({"name": "x", "value": 2}),
        ]
    )
    llm = LLM(_config(), backend=backend)
    result = await llm.complete_structured(
        layer="reasoning_heavy",
        system="sys",
        user="user",
        response_model=_FakeResponse,
    )
    assert result.value == 2
    # Two attempts consumed
    assert len(backend.calls) == 2
    # Retry message should have been appended to the messages list
    second_msgs = backend.calls[1]["messages"]
    assert isinstance(second_msgs, list)
    assert len(second_msgs) >= 4


@pytest.mark.asyncio
async def test_exhausting_retries_raises() -> None:
    backend = _ScriptedBackend(["bad", "bad", "bad"])
    llm = LLM(_config(), backend=backend)
    with pytest.raises(LLMError):
        await llm.complete_structured(
            layer="reasoning_heavy",
            system="s",
            user="u",
            response_model=_FakeResponse,
        )
    assert len(backend.calls) == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_token_budget_enforced() -> None:
    cfg = LLMConfig(
        reasoning_heavy=LayerConfig(
            model="test/fake",
            max_prompt_tokens=5,
            max_completion_tokens=100,
        )
    )
    backend = _ScriptedBackend([json.dumps({"name": "a", "value": 1})])
    llm = LLM(cfg, backend=backend)
    with pytest.raises(LLMError):
        await llm.complete_structured(
            layer="reasoning_heavy",
            system="this prompt is way too long",
            user="and the user side too",
            response_model=_FakeResponse,
        )
    assert backend.calls == []  # short-circuited before backend call


@pytest.mark.asyncio
async def test_token_usage_tracker() -> None:
    backend = _ScriptedBackend([json.dumps({"name": "a", "value": 1})])
    usage: list[CompletionResult] = []
    llm = LLM(_config(), backend=backend, token_usage=usage)
    await llm.complete_structured(
        layer="reasoning_heavy",
        system="s",
        user="u",
        response_model=_FakeResponse,
    )
    assert len(usage) == 1
    assert usage[0].total_tokens == 30
