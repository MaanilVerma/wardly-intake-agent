"""Thin Gemini client wrapper. Single source of truth for model config.

Two entry points cover the whole project:
  - chat_turn(history, system) -> str
        Live conversation path. Generates the next assistant utterance from
        the prior chat history. Used by the web chat route.
  - generate_structured(prompt, system, schema) -> dict
        Post-hoc extraction path. Generates a JSON object that conforms to
        the given Pydantic schema. Used by app.brief at end-of-intake.

The Gemini call itself is sync — it's one-shot per turn (or per finalize)
and the latency is dominated by the model, not the network.
"""

from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from typing import Literal, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Single source of model config. We default to gemini-3.1-flash-lite-preview —
# same JSON-Schema-constrained output as 2.5 flash, with 500 RPD on the free
# tier (vs 20 RPD on 2.5 flash / flash-lite as of April 2026). That gap
# matters when you're running eval tests + live calls + Loom takes off one
# key. The 3.x-preview models also have fresher capabilities (improved
# instruction following, better JSON adherence).
#
# The fallback chain runs in priority order on quota exhaustion:
#   1. gemini-3.1-flash-lite-preview  — 500 RPD, primary
#   2. gemini-3-flash                 — 20 RPD, secondary preview
#   3. gemini-2.5-flash-lite          — 20 RPD, stable
#   4. gemini-2.5-flash               — 20 RPD, stronger but smaller quota
#
# Models removed from the chain: gemini-2.0-flash and gemini-2.5-pro both
# show 0/0 RPD on this account (not allocated) — keeping them in only
# burned a network round-trip per fallback attempt.
DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"
FALLBACK_MODELS = (
    "gemini-3-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
)
DEFAULT_TEMPERATURE = 0.4
DEFAULT_MAX_OUTPUT_TOKENS = 600
# Bound any single Gemini call so a hung upstream can't tie up a request
# thread forever. Extraction needs longer than chat (more tokens, more rules).
CHAT_TIMEOUT_MS = 30_000
EXTRACT_TIMEOUT_MS = 60_000


class LLMQuotaExhausted(RuntimeError):
    """Every model in FALLBACK_MODELS returned 429 / RESOURCE_EXHAUSTED."""


class LLMUpstreamError(RuntimeError):
    """Non-quota failure talking to Gemini (network, schema, empty response)."""


@dataclass(frozen=True)
class ChatMessage:
    """One turn of dialog. role is who spoke."""
    role: Literal["user", "assistant"]
    content: str


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazily build a Gemini client. Reads GOOGLE_API_KEY from env each call
    in case the env was loaded after import (e.g. via python-dotenv)."""
    global _client
    if _client is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Add it to .env (see .env.example)."
            )
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=EXTRACT_TIMEOUT_MS),
        )
    return _client


def _history_to_contents(history: list[ChatMessage]) -> list[types.Content]:
    """Convert our role-tagged history into the Gemini SDK's Content shape.
    Gemini uses 'user' and 'model' rather than 'user'/'assistant'."""
    role_map = {"user": "user", "assistant": "model"}
    return [
        types.Content(role=role_map[m.role], parts=[types.Part(text=m.content)])
        for m in history
    ]


def chat_turn(
    history: list[ChatMessage],
    system: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> str:
    """Generate the next assistant utterance.

    `history` is the full prior dialog (user and assistant turns alternating,
    starting with the user). `system` is the conversation system prompt.
    Returns the assistant's plain-text reply. Falls back through FALLBACK_MODELS
    on quota exhaustion so a single saturated daily limit doesn't kill the chat.
    """
    if not history:
        raise ValueError("chat_turn requires at least one message in history")
    if history[-1].role != "user":
        raise ValueError("chat_turn expects history to end on a user turn")

    client = _get_client()
    contents = _history_to_contents(history)
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_exc: Exception | None = None
    response = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(
                model=candidate, contents=contents, config=config
            )
            if candidate != model:
                logger.warning("chat: primary %s exhausted; succeeded on fallback %s", model, candidate)
            break
        except Exception as e:
            last_exc = e
            if not _is_quota_error(e):
                raise
            logger.warning("chat: model %s hit quota: %s — trying next fallback", candidate, str(e)[:120])
    if response is None:
        raise LLMQuotaExhausted(
            f"All Gemini models hit quota (tried: {', '.join(candidates)}). "
            "Wait a few minutes; per-minute limits clear quickly. Daily limits reset at Pacific midnight."
        ) from last_exc

    text = (response.text or "").strip()
    if not text:
        raise LLMUpstreamError("Gemini returned an empty completion")
    return text


def _is_recoverable_model_error(exc: Exception) -> bool:
    """True for errors where falling back to the next model is the right move.

    Two cases:
      1. Quota exhausted (429 / RESOURCE_EXHAUSTED) — daily or per-minute limit hit.
      2. Model not found / not available (404 / NOT_FOUND / preview lapsed) —
         we may have a stale name in FALLBACK_MODELS. Skip and try next.

    Any other error (auth failure, schema invalid, network) propagates so we
    don't paper over real bugs.
    """
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
        return True
    if "NOT_FOUND" in msg or "404" in msg or "is not found" in msg.lower():
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in (404, 429)


# Keep the old name as an alias so existing call sites stay working without
# a rename pass — both point at the same predicate now.
_is_quota_error = _is_recoverable_model_error


def generate_structured(
    prompt: str,
    system: str,
    schema: Type[BaseModel],
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,  # lower temp for extraction — be deterministic
    max_output_tokens: int = 4096,
) -> dict:
    """Run a one-shot generation that returns a JSON object matching `schema`.

    The Pydantic class is passed directly to Gemini's `response_schema`; the
    SDK takes care of converting it to Gemini's structured-output format.
    Returns the parsed JSON dict — caller validates with the Pydantic model
    so we get a single, predictable error path on schema drift.

    On quota exhaustion (HTTP 429) we transparently fall back through
    FALLBACK_MODELS so a single saturated daily limit doesn't kill the brief.
    """
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=schema,
    )

    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_exc: Exception | None = None
    response = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(
                model=candidate, contents=prompt, config=config
            )
            if candidate != model:
                logger.warning("primary model %s exhausted; succeeded on fallback %s", model, candidate)
            break
        except Exception as e:
            last_exc = e
            if not _is_quota_error(e):
                raise
            logger.warning("model %s hit quota: %s — trying next fallback", candidate, str(e)[:120])
    if response is None:
        raise LLMQuotaExhausted(
            f"All Gemini models hit quota (tried: {', '.join(candidates)}). "
            "Wait a few minutes for the per-minute limit to clear, or for the "
            "daily limit to reset (Pacific midnight)."
        ) from last_exc

    # Prefer parsed object if the SDK already validated it; otherwise parse text.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, BaseModel):
        return parsed.model_dump(mode="json")

    text = response.text or ""
    if not text.strip():
        raise LLMUpstreamError("Gemini returned no JSON payload")
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Gemini returned invalid JSON: %r", text[:500])
        raise LLMUpstreamError(f"Gemini returned invalid JSON: {e}") from e


__all__ = [
    "ChatMessage",
    "chat_turn",
    "generate_structured",
    "DEFAULT_MODEL",
    "LLMQuotaExhausted",
    "LLMUpstreamError",
]
