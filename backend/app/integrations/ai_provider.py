"""Model provider adapters.

Spec 82.02 s6 and s23: providers are interchangeable infrastructure behind a common
interface, and frontends never reach one directly. Two implementations ship here:

* :class:`GeminiProvider` - the primary provider named in spec 82.01.
* :class:`StubProvider`   - a deterministic local provider. It is not a mock for
  tests only; it is how the concierge stays fully functional with no API key, no
  network and no spend. Because Mado's architecture requires that facts come from
  tools rather than the model (spec 56.01 s3.1), a provider that only has to
  *narrate* verified tool results can be genuinely useful without an LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger, redact

logger = get_logger("mado.ai.provider")


@dataclass(slots=True)
class GenerationRequest:
    system_prompt: str
    user_message: str
    context: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    temperature: float = 0.4
    max_output_tokens: int = 1024
    # Gemini 2.5 models reason before answering, and those thinking tokens are
    # drawn from the same budget as the answer. On a long prompt that silently
    # starved the response: the call succeeded, finishReason was STOP, and the
    # content came back truncated or empty. Extraction tasks gain nothing from it,
    # so they turn it off; open-ended generation leaves it on.
    thinking: bool = True


@dataclass(slots=True)
class GenerationResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    structured: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(Protocol):
    name: str

    async def generate(self, request: GenerationRequest, *, model: str) -> GenerationResult: ...


class GeminiProvider:
    """Adapter for the Gemini generateContent API."""

    name = "gemini"

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._base = "https://generativelanguage.googleapis.com/v1beta/models"

    async def generate(self, request: GenerationRequest, *, model: str) -> GenerationResult:
        contents = [
            {
                "role": "user" if turn["role"] == "user" else "model",
                "parts": [{"text": turn["content"]}],
            }
            for turn in request.history
        ]
        contents.append({"role": "user", "parts": [{"text": request.user_message}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        # Structured output is preferred over parsing prose whenever a schema
        # exists (spec 56.01 s9).
        if request.response_schema is not None:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = request.response_schema

        if not request.thinking:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        url = f"{self._base}/{model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    # Header auth, not `?key=`: httpx renders the full URL into the
                    # text of any transport error, which would put the key in logs.
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self._api_key,
                    },
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("gemini_request_failed", error=redact(str(exc)), model=model)
            raise ServiceUnavailableError(
                "The assistant is temporarily unavailable.", code="AI_PROVIDER_UNAVAILABLE"
            ) from exc

        usage = body.get("usageMetadata", {})
        candidate = (body.get("candidates") or [{}])[0]
        # Carried into every failure log below. A truncated or empty response is
        # almost always a budget or safety outcome rather than a malformed model,
        # and without these fields the two are indistinguishable from the outside.
        diagnostics = {
            "model": model,
            "finish_reason": candidate.get("finishReason"),
            "thought_tokens": usage.get("thoughtsTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            "max_output_tokens": request.max_output_tokens,
        }

        try:
            text = candidate["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            logger.warning("gemini_empty_response", **diagnostics)
            text = ""

        structured = None
        if request.response_schema is not None and text:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "gemini_structured_parse_failed", **diagnostics, preview=text[:120]
                )

        return GenerationResult(
            text=text,
            model=model,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            structured=structured,
        )


class StubProvider:
    """Deterministic offline provider.

    Composes a reply from the tool results already assembled by the orchestrator.
    Every fact it states was retrieved from the platform, so its answers are
    accurate - they are simply less fluent than a model's.
    """

    name = "stub"

    async def generate(self, request: GenerationRequest, *, model: str) -> GenerationResult:
        context = request.context or {}
        results = context.get("results") or []
        intent = context.get("intent", "GENERAL_ASSISTANCE")

        if request.response_schema is not None:
            # Intent classification path - handled by the rule-based classifier, so
            # echoing the pre-computed decision keeps one source of truth.
            return GenerationResult(
                text=json.dumps(context.get("structured", {})),
                model=f"{model}-stub",
                structured=context.get("structured", {}),
            )

        text = _compose_offline_reply(intent=intent, results=results, context=context)
        return GenerationResult(text=text, model=f"{model}-stub")


def _describe(item: dict[str, Any]) -> str:
    """One human line per result, using only retrieved fields."""
    parts = [item.get("title", "An experience")]

    venue = item.get("venue_name")
    if venue:
        parts.append(f"at {venue}")

    when = item.get("when")
    if when:
        parts.append(when)

    price = item.get("price")
    if price:
        parts.append(f"({price})")

    line = " ".join(parts)
    reason = item.get("reason")
    return f"{line} - {reason}" if reason else line


def _compose_offline_reply(*, intent: str, results: list[dict], context: dict) -> str:
    city = context.get("city_name", "the city")
    time_label = context.get("time_label")

    if not results:
        if time_label:
            return (
                f"Nothing is scheduled in {city} {time_label}. "
                "Want me to look at tomorrow, or at places that are open anyway?"
            )
        return (
            f"I could not find anything in {city} matching that. "
            "Try fewer words, or ask me for something nearby instead."
        )

    # A stated time window is the most useful thing to reflect back, so it takes
    # precedence over the intent-derived phrasing.
    if time_label:
        opener = f"Here is what is on {time_label} in {city}:"
    else:
        openers = {
            "DISCOVER_EVENTS": f"Here is what is happening in {city}:",
            "SEARCH_EVENTS": f"Here is what I found in {city}:",
            "SEARCH_EXPERIENCES": f"Here is what I found in {city}:",
            "RECOMMEND_ACTIVITY": "Based on what you have told me, these look like a good fit:",
            "PLAN_ACTIVITY": "Here is a plan you could follow:",
            "GET_EVENT_DETAILS": "Here are the details:",
        }
        opener = openers.get(intent, f"Here is what I found in {city}:")

    lines = [f"{index}. {_describe(item)}" for index, item in enumerate(results[:5], start=1)]
    closing = (
        "Tell me if you want these closer to you, cheaper, or at a different time."
        if intent != "GET_EVENT_DETAILS"
        else "Ask me anything else about it."
    )
    return "\n".join([opener, "", *lines, "", closing])


@lru_cache
def get_provider() -> LLMProvider:
    settings = get_settings()
    if settings.ai_provider == "gemini":
        if not settings.gemini_api_key:
            # Falling back is better than failing: the concierge stays usable and
            # the misconfiguration is logged loudly rather than crashing startup.
            logger.warning("gemini_selected_without_key_falling_back_to_stub")
            return StubProvider()
        return GeminiProvider(settings.gemini_api_key, timeout=settings.ai_request_timeout_seconds)
    return StubProvider()
