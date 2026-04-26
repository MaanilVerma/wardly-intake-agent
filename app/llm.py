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

# Single source of model config. We default to gemini-2.5-flash-lite — same
# JSON-Schema-constrained output as flash, but the free-tier daily quota is
# ~10x higher (1000 RPD vs ~20-250 RPD on flash), which actually matters for
# a take-home that runs eval tests + live calls + Loom takes off one key.
# If quality on a specific call disappoints, override per-call via the
# `model=` kwarg on chat_turn / generate_structured.
DEFAULT_MODEL = "gemini-2.5-flash-lite"
FALLBACK_MODELS = ("gemini-2.0-flash", "gemini-2.5-flash")
DEFAULT_TEMPERATURE = 0.4
DEFAULT_MAX_OUTPUT_TOKENS = 600


@dataclass(frozen=True)
class ChatMessage:
    """One turn of dialog. role is who spoke."""
    role: Literal["user", "assistant"]
    content: str


# --- client ----------------------------------------------------------------

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
        _client = genai.Client(api_key=api_key)
    return _client


# --- conversation ----------------------------------------------------------

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
        raise RuntimeError(
            f"All Gemini models hit quota (tried: {', '.join(candidates)}). "
            "Wait a few minutes; per-minute limits clear quickly. Daily limits reset at Pacific midnight."
        ) from last_exc

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty completion")
    return text


# --- structured extraction --------------------------------------------------

def _is_quota_error(exc: Exception) -> bool:
    """True if this looks like a 429 / quota exhausted error from the Gemini SDK."""
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code == 429


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
        # All candidates exhausted.
        raise RuntimeError(
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
        raise RuntimeError("Gemini returned no JSON payload")
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Gemini returned invalid JSON: %r", text[:500])
        raise RuntimeError(f"Gemini returned invalid JSON: {e}") from e


__all__ = [
    "ChatMessage",
    "chat_turn",
    "generate_structured",
    "DEFAULT_MODEL",
]
