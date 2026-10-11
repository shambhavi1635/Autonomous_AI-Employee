"""Thin adapter over the Google Gemini API.

Why an adapter instead of calling the SDK directly from the planner?

1. Swappability. Every provider-specific detail is confined to this file.
   Changing to OpenAI or a local model touches one file, not the whole agent.
2. Retries in one place. Transient 429/503 errors are a fact of life with
   LLM APIs. Handling them here means no caller needs to care.
3. Structured output in one place. The agent's planner must return a typed
   Plan, never prose. generate_structured() is the only way we ask the model
   for data, and it validates the result through Pydantic before returning.
4. Observability. Token usage is captured per call so we can report the cost
   of a run.

Deliberately NOT here: prompts (they live in llm/prompts/), agent logic
(core/), or any knowledge of what a Plan is. This file knows how to talk to
a model and nothing about the task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from core import config

T = TypeVar("T", bound=BaseModel)

# Substrings that mark an error as worth retrying. Anything else (bad API
# key, malformed request, safety block) is a real bug or a real refusal --
# retrying it would only waste time and hide the cause.
_TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "resource_exhausted",
    "unavailable",
    "deadline",
    "timeout",
    "internal error",
)


class LLMError(RuntimeError):
    """Raised when a model call cannot be completed."""


@dataclass
class Usage:
    """Token accounting for a single call, accumulated across a run."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, prompt: int, output: int) -> None:
        self.prompt_tokens += prompt
        self.output_tokens += output
        self.calls += 1


@dataclass
class LLMClient:
    """Wraps a Gemini client with retries, structured output and usage stats."""

    api_key: str | None = None
    usage: Usage = field(default_factory=Usage)
    _client: genai.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        key = self.api_key or config.require_api_key()
        self._client = genai.Client(api_key=key)

    # ------------------------------------------------------------ discovery
    def list_models(self) -> list[str]:
        """Return the model IDs this API key can actually use.

        Google's model names change frequently and differ by account, so we
        discover them rather than hardcoding a guess.
        """
        names: list[str] = []
        for model in self._client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            name = (model.name or "").removeprefix("models/")
            if name:
                names.append(name)
        return sorted(names)

    # ----------------------------------------------------------- generation
    def generate_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        """Free-form text generation. Used for explanations, not for data."""
        response = self._call(
            prompt,
            model=model or config.PLANNER_MODEL,
            system=system,
            temperature=temperature,
            schema=None,
        )
        text = (response.text or "").strip()
        if not text:
            raise LLMError("Model returned an empty response.")
        return text

    def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T:
        """Generation constrained to a Pydantic schema.

        This is how the planner, verifier and reflector talk to the model.
        Passing the schema to the API constrains decoding to valid JSON, so
        we are not parsing prose with regexes. We still re-validate with
        Pydantic, because a schema-constrained response is not a guarantee.

        temperature defaults to 0.0: for structured decisions we want the
        most likely answer and reproducible behaviour while debugging.
        """
        response = self._call(
            prompt,
            model=model or config.PLANNER_MODEL,
            system=system,
            temperature=temperature,
            schema=schema,
        )

        # The SDK parses the response into the schema for us when it can.
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed

        raw = (response.text or "").strip()
        if not raw:
            raise LLMError(f"Model returned nothing for schema {schema.__name__}.")

        # Fallback: some model/tool combinations wrap JSON in a markdown
        # fence despite the schema being set. Strip it and validate manually.
        raw = _strip_code_fence(raw)

        try:
            return schema.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMError(
                f"Model output did not match {schema.__name__}.\n"
                f"--- raw output ---\n{raw[:2000]}\n--- error ---\n{exc}"
            ) from exc

    # ------------------------------------------------------------ internals
    def _call(
        self,
        prompt: str,
        *,
        model: str,
        system: str | None,
        temperature: float,
        schema: type[BaseModel] | None,
    ) -> Any:
        """Single API call with exponential backoff on transient failures."""
        cfg: dict[str, Any] = {"temperature": temperature}
        if system:
            cfg["system_instruction"] = system
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = schema

        last_error: Exception | None = None
        for attempt in range(config.LLM_MAX_RETRIES):
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**cfg),
                )
            except Exception as exc:  # SDK raises provider-specific types
                last_error = exc
                if not _is_transient(exc):
                    raise LLMError(f"Model call failed ({model}): {exc}") from exc
                if attempt == config.LLM_MAX_RETRIES - 1:
                    break
                time.sleep(2**attempt)  # 1s, 2s, 4s ...
                continue

            self._record_usage(response)
            return response

        raise LLMError(
            f"Model call to {model} failed after {config.LLM_MAX_RETRIES} "
            f"attempts: {last_error}"
        ) from last_error

    def _record_usage(self, response: Any) -> None:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return
        self.usage.add(
            prompt=getattr(meta, "prompt_token_count", 0) or 0,
            output=getattr(meta, "candidates_token_count", 0) or 0,
        )


def _is_transient(exc: Exception) -> bool:
    """Decide whether an error is worth retrying."""
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(marker in blob for marker in _TRANSIENT_MARKERS)


def _strip_code_fence(raw: str) -> str:
    """Remove a surrounding markdown code fence, if present."""
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    lines = lines[1:]  # drop the opening ```json line
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
