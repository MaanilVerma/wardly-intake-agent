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
# The fallback chain — stable models first, big-quota-but-unstable last.
#   1. gemini-2.5-flash-lite          — 20 RPD, stable. Worked cleanly on
#                                       every recovery run we tested. Default.
#   2. gemini-2.5-flash               — 20 RPD, stable, stronger reasoning.
#   3. gemini-3.1-flash-lite-preview  — 500 RPD but preview-marked and prone
#                                       to 503 storms. Last resort: when both
#                                       2.5 models hit 429 daily limit, this
#                                       has fresh capacity.
#
# Models removed from the chain (verified empirically on 2026-04-27):
#   - gemini-3-flash: returns 404 NOT_FOUND on v1beta. The dashboard
#     shows "Gemini 3 Flash" with quota allocated but the API rejects the
#     string. Likely a different API name we don't know yet, or paid-tier
#     only. Burning a network round-trip per fallback attempt.
#   - gemini-2.0-flash, gemini-2.5-pro: 0/0 RPD on this account.
DEFAULT_MODEL = "gemini-2.5-flash-lite"
FALLBACK_MODELS = (
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
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
            if not _is_recoverable_model_error(e):
                raise
            logger.warning(
                "chat: model %s returned a recoverable error (%s) — trying next fallback",
                candidate, str(e)[:140],
            )
    if response is None:
        raise LLMQuotaExhausted(
            f"All Gemini models in the chain failed (tried: {', '.join(candidates)}). "
            "Last error: " + (str(last_exc)[:200] if last_exc else "unknown") + ". "
            "Causes can be quota (429), upstream overload (503), or model-not-found (404). "
            "Per-minute quota limits clear quickly; daily limits reset at Pacific midnight; "
            "503 storms are usually transient — retry in a minute."
        ) from last_exc

    text = (response.text or "").strip()
    if not text:
        raise LLMUpstreamError("Gemini returned an empty completion")
    return text


def _is_recoverable_model_error(exc: Exception) -> bool:
    """True for errors where falling back to the next model is the right move.

    Cases that justify a rotation:
      1. **Quota exhausted** — 429 / RESOURCE_EXHAUSTED. Daily or per-minute
         limit hit on this model; another model in the chain may have capacity.
      2. **Model overloaded** — 503 UNAVAILABLE. The model is fine, just
         drowning in demand. Another model in the chain is almost certainly
         not having the same exact spike at the same exact moment.
      3. **Other transient upstream** — 500 INTERNAL, 502 BAD_GATEWAY,
         504 DEADLINE_EXCEEDED. Same logic: rotate, don't fail.
      4. **Model not found** — 404 / NOT_FOUND / preview lapsed. We may
         have a stale name in FALLBACK_MODELS. Skip and try next.

    Errors that should NOT be retried (and intentionally propagate):
      - 400 INVALID_ARGUMENT — schema problem, prompt problem, real bug
      - 401 / 403 — auth failure, won't fix on retry
      - Network errors that aren't HTTP — handled at a layer above
    """
    msg = str(exc)
    msg_lower = msg.lower()

    # String-shape detection (the SDK formats errors as "<code> <NAME>. {...}")
    transient_tokens = (
        "RESOURCE_EXHAUSTED",  # 429 — quota
        "UNAVAILABLE",         # 503 — overloaded
        "DEADLINE_EXCEEDED",   # 504 — timed out
        "INTERNAL",            # 500 — generic Google-side fault
        "BAD_GATEWAY",         # 502
        "NOT_FOUND",           # 404 — bad/expired model name
    )
    for token in transient_tokens:
        if token in msg:
            return True
    if "is not found" in msg_lower:
        return True

    # Numeric-code detection (when the SDK exposes status_code / code on the exception)
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in (404, 429, 500, 502, 503, 504)


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
            if not _is_recoverable_model_error(e):
                raise
            logger.warning(
                "model %s returned a recoverable error (%s) — trying next fallback",
                candidate, str(e)[:140],
            )
    if response is None:
        raise LLMQuotaExhausted(
            f"All Gemini models in the chain failed (tried: {', '.join(candidates)}). "
            "Last error: " + (str(last_exc)[:200] if last_exc else "unknown") + ". "
            "Causes can be quota (429), upstream overload (503), or model-not-found (404). "
            "Retry in a minute for transient storms; daily limits reset at Pacific midnight."
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
