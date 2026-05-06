"""LLM router for the reasoning, intention, and reflection layers.

Thin wrapper around LiteLLM that enforces:

- **Structured outputs only.** Every call is parameterised by a Pydantic
  response model and the wire content is validated against it.
- **Retry on malformed output.** Up to two retries are attempted on
  JSON parse / validation failures.
- **Per-call token budget.** The caller may cap both prompt and completion
  tokens; budget violations raise :class:`LLMError` before the network call.
- **Per-layer model configuration.** The active model is resolved by layer
  name from :class:`LLMConfig` — users change providers by editing their
  config, never by editing code.

Calls are routed through :func:`litellm.acompletion` so any provider
supported by LiteLLM (OpenAI, Anthropic, Ollama, etc.) works transparently.
"""

from __future__ import annotations

import json
import os
from typing import Literal, Protocol, TypeVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from coremind.errors import LLMError

log = structlog.get_logger(__name__)

type Layer = Literal["reasoning_heavy", "reasoning_fast", "intention", "reflection"]

T = TypeVar("T", bound=BaseModel)

_MAX_RETRIES = 2


class LayerConfig(BaseModel):
    """Per-layer LLM settings.

    Attributes:
        model: LiteLLM-style model identifier
            (e.g. ``openai/gpt-4o``, ``ollama/glm-5.1``).
        max_prompt_tokens: Upper bound on the rendered prompt tokens.
            Calls exceeding this budget are rejected.  ``None`` disables
            the check.
        max_completion_tokens: Max completion tokens requested from the model.
        temperature: Sampling temperature.
        api_key_env: Name of the environment variable holding the API key,
            if the provider requires one.  The value is injected as the
            ``api_key`` argument to LiteLLM at call time; the key itself is
            never stored in config.
    """

    model: str
    max_prompt_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int = Field(default=2048, ge=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    api_key_env: str | None = None


class LLMConfig(BaseModel):
    """Complete LLM routing configuration for all four layers."""

    reasoning_heavy: LayerConfig = Field(
        default_factory=lambda: LayerConfig(model="ollama/deepseek-v4-flash:cloud")
    )
    reasoning_fast: LayerConfig = Field(
        default_factory=lambda: LayerConfig(model="ollama/mistral-large-3:675b-cloud")
    )
    intention: LayerConfig = Field(
        default_factory=lambda: LayerConfig(model="ollama/mistral-large-3:675b-cloud")
    )
    reflection: LayerConfig = Field(
        default_factory=lambda: LayerConfig(model="ollama/deepseek-v4-flash:cloud")
    )


# ---------------------------------------------------------------------------
# Completion backend port — injectable so tests don't hit the network
# ---------------------------------------------------------------------------


class CompletionBackend(Protocol):
    """Port for an async chat-completion backend.

    Satisfied by :func:`litellm.acompletion` wrapped in
    :class:`LiteLLMBackend` and by in-process fakes in tests.
    """

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
        """Produce a completion and usage stats.

        Returns:
            A :class:`CompletionResult` carrying the raw text and token counts.

        Raises:
            LLMError: On any transport / provider failure.
        """
        ...


class CompletionResult(BaseModel):
    """Normalised result from a :class:`CompletionBackend`."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LiteLLMBackend:
    """Default :class:`CompletionBackend` implemented via LiteLLM."""

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
        """Call :func:`litellm.acompletion` and normalise the response."""
        import litellm  # noqa: PLC0415 — heavy optional dep

        kwargs: dict[str, object] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Pass Ollama base URL explicitly — LiteLLM may not read OLLAMA_API_BASE reliably
        ollama_url = os.environ.get("OLLAMA_API_BASE")
        if ollama_url and model.startswith("ollama/"):
            kwargs["api_base"] = ollama_url
        if response_format is not None:
            kwargs["response_format"] = response_format
        if api_key is not None:
            kwargs["api_key"] = api_key

        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise LLMError(f"LiteLLM call to {model!r} failed: {exc}") from exc

        try:
            choice = response["choices"][0]
            content = choice["message"]["content"] or ""
            usage = response.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LiteLLM returned malformed response for {model!r}") from exc

        return CompletionResult(
            content=str(content),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
        )


# ---------------------------------------------------------------------------
# LLM wrapper
# ---------------------------------------------------------------------------


def _approx_token_count(text: str) -> int:
    """Approximate a token count for budget-enforcement purposes.

    Uses the common 4-chars-per-token heuristic; precise tokenisation is
    provider-specific and not worth the dependency for a pre-flight check.

    Args:
        text: The text to estimate.

    Returns:
        Approximate token count (ceiling of len/4).
    """
    if not text:
        return 0
    return (len(text) + 3) // 4


def _strip_json_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from an LLM response.

    Some models wrap JSON in ```json ... ``` despite instructions.  This
    helper tolerates that without masking actual malformed output — any
    content that is still not parseable after stripping raises on the
    subsequent :func:`json.loads` call.
    """
    s = text.strip()
    if s.startswith("```"):
        # drop the first fence line
        s = s.split("\n", 1)[1] if "\n" in s else ""
    if s.endswith("```"):
        s = s.rsplit("```", 1)[0]
    return s.strip()


class LLM:
    """Layer-aware wrapper around a :class:`CompletionBackend`.

    Args:
        config: Per-layer model configuration.
        backend: Completion backend.  Defaults to :class:`LiteLLMBackend`.
            Inject a fake in tests to avoid network access.
        token_usage: List that accumulates a :class:`CompletionResult` per
            successful call.  Optional — provide when the caller wants to
            audit cumulative token spend across a cycle.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        backend: CompletionBackend | None = None,
        token_usage: list[CompletionResult] | None = None,
    ) -> None:
        self._config = config
        self._backend: CompletionBackend = backend or LiteLLMBackend()
        self._usage: list[CompletionResult] | None = token_usage

    @property
    def config(self) -> LLMConfig:
        """Return the underlying layer configuration."""
        return self._config

    def _layer_config(self, layer: Layer) -> LayerConfig:
        """Return the :class:`LayerConfig` for *layer*."""
        cfg: LayerConfig = getattr(self._config, layer)
        return cfg

    async def complete_structured(
        self,
        layer: Layer,
        system: str,
        user: str,
        response_model: type[T],
        *,
        max_tokens: int | None = None,
    ) -> T:
        """Obtain a structured response validated against *response_model*.

        Applies a pre-flight token budget check, then calls the backend with
        JSON response format enforced.  Retries up to :data:`_MAX_RETRIES`
        times on parse or validation failures, appending a correction note
        to the user message each retry.

        Args:
            layer: Which layer's model configuration to use.
            system: System-role prompt.
            user: User-role prompt (contains snapshot/context).
            response_model: Pydantic model the response must validate against.
            max_tokens: Override for completion token limit.  Defaults to
                the layer's ``max_completion_tokens``.

        Returns:
            A validated instance of ``response_model``.

        Raises:
            LLMError: If the prompt exceeds budget, the backend fails, or
                the response cannot be validated after retries.
        """
        cfg = self._layer_config(layer)

        # Pre-flight budget check
        if cfg.max_prompt_tokens is not None:
            approx = _approx_token_count(system) + _approx_token_count(user)
            if approx > cfg.max_prompt_tokens:
                raise LLMError(
                    f"prompt token estimate {approx} exceeds layer "
                    f"{layer!r} budget {cfg.max_prompt_tokens}"
                )

        completion_cap = max_tokens if max_tokens is not None else cfg.max_completion_tokens
        api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = await self._backend.complete(
                    model=cfg.model,
                    messages=messages,
                    max_tokens=completion_cap,
                    temperature=cfg.temperature,
                    response_format={"type": "json_object"},
                    api_key=api_key,
                )
            except LLMError:
                raise
            except Exception as exc:
                raise LLMError(f"backend call failed for layer {layer!r}") from exc

            log.info(
                "llm.call",
                layer=layer,
                model=cfg.model,
                attempt=attempt + 1,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

            content = _strip_json_fences(result.content)
            try:
                raw = json.loads(content)
                validated = response_model.model_validate(raw)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                log.warning(
                    "llm.malformed_output",
                    layer=layer,
                    model=cfg.model,
                    attempt=attempt + 1,
                    error=str(exc)[:200],
                )
                if attempt < _MAX_RETRIES:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": result.content},
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON matching "
                                f"the required schema. Error: {exc}. "
                                "Return ONLY a single valid JSON object, no prose."
                            ),
                        },
                    ]
                    continue
                break

            if self._usage is not None:
                self._usage.append(result)
            return validated

        raise LLMError(
            f"LLM returned malformed structured output for layer {layer!r} "
            f"after {_MAX_RETRIES + 1} attempts: {last_error}"
        )

    async def complete_text(
        self,
        layer: Layer,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Obtain a plain-text response — no JSON formatting enforced.

        Use this for free-form conversation where JSON mode would degrade quality.
        """
        cfg = self._layer_config(layer)
        completion_cap = max_tokens if max_tokens is not None else cfg.max_completion_tokens
        api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        try:
            result = await self._backend.complete(
                model=cfg.model,
                messages=messages,
                max_tokens=completion_cap,
                temperature=0.8,  # Higher temp for creative conversation
                response_format=None,  # No JSON — free text
                api_key=api_key,
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"backend call failed for layer {layer!r}") from exc

        log.info(
            "llm.call",
            layer=layer,
            model=cfg.model,
            attempt=1,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result.content.strip()
