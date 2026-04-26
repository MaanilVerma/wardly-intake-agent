"""Vapi voice webhook handlers (bonus transport).

Two endpoints, both POST'd by Vapi with body shape `{"message": {...}}`:
  - /webhook/tool-call   — synchronous response. Acknowledges flag_red_flag
                            calls; the response shape is strict (string
                            result, single-line, HTTP 200 always).
  - /webhook/end-of-call — receives the transcript + call metadata at
                            hangup. Schedules brief extraction in the
                            background and returns 200 immediately.

The same `extract_from_transcript` powers both transports — voice and web
share one extraction path.
"""

from __future__ import annotations
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Request

from app.brief import extract_from_transcript
from app.models import RedFlag
from app.storage import save_json, save_markdown, save_transcript

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["vapi"])

# Optional shared secret for Vapi webhooks. When set, every request must carry
# the same value in `X-Vapi-Secret` (or the legacy `x-vapi-signature` header)
# or it's rejected. Unset is allowed for local dev/take-home; we log a single
# WARNING at startup so it's clear we're running unauthenticated.
_VAPI_WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "")
if not _VAPI_WEBHOOK_SECRET:
    logger.warning(
        "VAPI_WEBHOOK_SECRET is not set — /webhook/* endpoints accept any caller. "
        "Set it in .env for any non-local deployment."
    )


def _verify_vapi_secret(request: Request) -> bool:
    """Return True if the request is allowed.

    No secret configured → allow (local dev). Secret set → require it on the
    request via header. Constant-time compare to avoid timing oracles.
    """
    if not _VAPI_WEBHOOK_SECRET:
        return True
    presented = (
        request.headers.get("x-vapi-secret")
        or request.headers.get("x-vapi-signature")
        or ""
    )
    return hmac.compare_digest(presented, _VAPI_WEBHOOK_SECRET)


async def _read_json_safe(request: Request) -> Optional[dict]:
    """Parse the request body as JSON. Return None on any parse failure
    so callers can ack with a 200 (Vapi drops anything else)."""
    try:
        return await request.json()
    except Exception as e:
        logger.warning("webhook body was not valid JSON: %s", str(e)[:120])
        return None


# ---- in-memory red-flag store --------------------------------------------
# Keyed by Vapi `call.id`. Tool-call events arrive mid-call; end-of-call
# pulls these out and merges them with whatever the extractor saw in the
# transcript. Single-process / dict — same scope as our chat sessions.

_redflags_lock = threading.Lock()
_redflags: dict[str, list[RedFlag]] = {}


def _record_red_flag(call_id: str, flag: RedFlag) -> None:
    with _redflags_lock:
        _redflags.setdefault(call_id, []).append(flag)


def _drain_red_flags(call_id: str) -> list[RedFlag]:
    with _redflags_lock:
        return _redflags.pop(call_id, [])


# ---- Vapi response shape (tool-calls) -------------------------------------
# Vapi requires:
#   - body: {"results": [{"toolCallId": "<echoed id>", "result": "<string>"}]}
#   - result and error MUST be strings (not objects/arrays)
#   - single-line strings — line breaks are parse errors on Vapi's side
#   - HTTP 200 always, even on errors
# Source: docs.vapi.ai/tools/custom-tools


def _tool_results(items: list[dict[str, str]]) -> dict:
    """Build the tool-call response shape, normalizing each result to a
    single-line string per Vapi's strict parser."""
    out: list[dict] = []
    for item in items:
        out.append({
            "toolCallId": item["toolCallId"],
            "result": item["result"].replace("\n", " ").strip(),
        })
    return {"results": out}


def _extract_tool_call_list(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Vapi has used multiple field names over time: `toolCallList` (older)
    and `toolCalls` (newer, OpenAI-style). Accept either."""
    if isinstance(message.get("toolCallList"), list):
        return message["toolCallList"]
    if isinstance(message.get("toolCalls"), list):
        return message["toolCalls"]
    return []


def _safe_json_loads(s: str | dict | None) -> dict[str, Any]:
    """Tool-call arguments are usually a JSON-string; sometimes (depending on
    Vapi version) already a dict. Be permissive."""
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        logger.warning("could not parse tool-call arguments: %r", s[:200])
        return {}


def _extract_transcript(message: dict[str, Any]) -> str:
    """End-of-call payload contains transcript at message.artifact.transcript
    (single string) and message.artifact.messages (role+content turns).
    Prefer the rendered string; fall back to messages."""
    artifact = message.get("artifact") or {}
    transcript = artifact.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return transcript

    msgs = artifact.get("messages") or message.get("messages") or []
    lines: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").lower()
        content = m.get("message") or m.get("content") or ""
        if not content:
            continue
        prefix = "Agent" if role in ("assistant", "bot", "tool") else "Patient"
        lines.append(f"{prefix}: {content}".strip())
    return "\n".join(lines)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        # Vapi uses ISO-8601 with 'Z' or offset.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.post("/tool-call")
async def tool_call(request: Request) -> dict:
    """Synchronously ack tool calls. Records flag_red_flag invocations so the
    end-of-call handler can merge them into the final brief.

    Always returns HTTP 200 — Vapi silently drops any other status code.
    """
    if not _verify_vapi_secret(request):
        logger.warning("tool-call rejected: bad/missing webhook secret")
        return _tool_results([])
    body = await _read_json_safe(request)
    if body is None:
        return _tool_results([])
    logger.debug("tool-call payload: %s", json.dumps(body)[:2000])

    message = body.get("message") or {}
    call = message.get("call") or {}
    call_id = call.get("id") or ""

    items = _extract_tool_call_list(message)
    if not items:
        logger.warning("tool-call message had no tool calls")
        return _tool_results([])

    out_items: list[dict[str, str]] = []
    for item in items:
        tool_call_id = item.get("id") or ""
        fn = item.get("function") or {}
        name = fn.get("name") or ""
        args = _safe_json_loads(fn.get("arguments"))

        if name == "flag_red_flag":
            symptom = (args.get("symptom") or "").strip()
            severity = (args.get("severity") or "concern").strip().lower()
            advised = (args.get("advised_action") or None)
            if severity not in ("concern", "urgent", "emergent"):
                severity = "concern"
            try:
                flag = RedFlag(
                    symptom=symptom or "(unspecified)",
                    severity=severity,  # type: ignore[arg-type]
                    advised_action=advised,
                )
                if call_id:
                    _record_red_flag(call_id, flag)
                logger.info(
                    "red flag recorded: call=%s severity=%s symptom=%r",
                    call_id, severity, symptom,
                )
                out_items.append({"toolCallId": tool_call_id, "result": "acknowledged"})
            except Exception as e:
                logger.warning("invalid flag_red_flag args: %s", e)
                out_items.append({"toolCallId": tool_call_id, "result": "invalid arguments"})
        else:
            logger.warning("unknown tool: %s", name)
            out_items.append({"toolCallId": tool_call_id, "result": f"unknown tool: {name}"})

    return _tool_results(out_items)


@router.post("/end-of-call")
async def end_of_call(request: Request, background: BackgroundTasks) -> dict:
    """Receive the end-of-call report. Save the transcript synchronously
    (cheap, no LLM), schedule brief extraction in the background, and
    return 200 immediately so Vapi doesn't hit its webhook timeout."""
    if not _verify_vapi_secret(request):
        logger.warning("end-of-call rejected: bad/missing webhook secret")
        return {"status": "ok"}
    body = await _read_json_safe(request)
    if body is None:
        return {"status": "ok"}
    logger.debug("end-of-call payload: %s", json.dumps(body)[:2000])

    message = body.get("message") or {}
    if message.get("type") != "end-of-call-report":
        # Vapi sends status-update via the same serverUrl. Acknowledge but
        # don't try to extract.
        logger.info("ignoring non-end-of-call message: %s", message.get("type"))
        return {"status": "ok"}

    call = message.get("call") or {}
    call_id = call.get("id") or ""
    if not call_id:
        logger.warning("end-of-call: missing call.id, cannot persist")
        return {"status": "ok"}

    transcript = _extract_transcript(message)
    if not transcript.strip():
        logger.warning("end-of-call: transcript missing for call %s", call_id)
        return {"status": "ok"}

    started_at = _parse_iso(message.get("startedAt") or call.get("startedAt"))
    ended_at = _parse_iso(message.get("endedAt") or call.get("endedAt")) or datetime.now(timezone.utc)

    # Persist the transcript synchronously — it's the source of truth for
    # any later re-extraction or debugging, and shouldn't depend on the
    # background task succeeding.
    save_transcript(call_id, transcript)

    background.add_task(
        _generate_and_save_brief,
        call_id=call_id,
        transcript=transcript,
        started_at=started_at,
        ended_at=ended_at,
    )
    return {"status": "ok"}


def _generate_and_save_brief(
    *,
    call_id: str,
    transcript: str,
    started_at: Optional[datetime],
    ended_at: Optional[datetime],
) -> None:
    """Background-only — never call from a request handler.
    Errors are logged, never raised, so a single bad call doesn't poison
    the worker thread."""
    try:
        brief = extract_from_transcript(
            transcript,
            call_id=call_id,
            started_at=started_at,
            ended_at=ended_at,
        )

        # Merge any red flags captured live via tool-call. Dedup by symptom
        # (case-insensitive) so we don't double-flag the same thing the
        # extractor already saw in the transcript.
        live_flags = _drain_red_flags(call_id)
        if live_flags:
            seen = {rf.symptom.lower() for rf in brief.red_flags}
            merged = list(brief.red_flags)
            for f in live_flags:
                if f.symptom.lower() not in seen:
                    merged.append(f)
                    seen.add(f.symptom.lower())
            brief = brief.model_copy(update={"red_flags": merged})

        save_json(call_id, brief.model_dump(mode="json"))
        save_markdown(call_id, brief.to_markdown())
        logger.info("brief generated for call %s", call_id)
    except Exception:
        logger.exception("brief generation failed for call %s", call_id)


__all__ = ["router"]
