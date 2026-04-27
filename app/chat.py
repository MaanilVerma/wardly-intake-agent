"""Web chat session + routes.

In-memory session state (single-process). For a take-home this is the right
amount of complexity — sessions don't survive a restart, but neither does
the demo. If we ever needed to scale, swap the dict for Redis without
changing the route shape.

The transcript format we emit at finalize matches the format of the test
fixtures so the same `extract_from_transcript` works for both transports.
"""

from __future__ import annotations
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from app.brief import extract_from_transcript
from app.llm import ChatMessage, LLMQuotaExhausted, LLMUpstreamError, chat_turn
from app.models import RedFlag
from app.prompts import SYSTEM_PROMPT
from app.storage import save_json, save_markdown, save_transcript

logger = logging.getLogger(__name__)

# The greeting matches Vapi's `firstMessage`. Hardcoding it (rather than
# generating from the system prompt) saves an LLM round-trip on connect
# and gives us identical patient-facing copy across both transports.
# Deliberately short and action-first — no "I'd like to ask a few questions"
# (forbidden phrase per the system prompt), no clipboard preamble.
GREETING = (
    "Hi, this is Maria from the clinic — quick check before your visit, "
    "takes about five minutes. Is now okay?"
)


@dataclass
class Session:
    session_id: str
    started_at: datetime
    history: list[ChatMessage] = field(default_factory=list)
    # Red flags captured live from leaked tool-call syntax in agent replies
    # (web chat doesn't register tools with Gemini, so the model sometimes
    # writes flag_red_flag(...) as text). Merged into the brief at finalize
    # alongside extractor-derived flags.
    red_flags: list[RedFlag] = field(default_factory=list)


class _SessionStore:
    """Thread-safe in-memory session store."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> Session:
        sid = uuid.uuid4().hex
        sess = Session(session_id=sid, started_at=datetime.now(timezone.utc))
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, session_id: str) -> Session:
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id} not found",
            )
        return sess

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


_store = _SessionStore()


class StartResponse(BaseModel):
    session_id: str
    first_message: str


class MessageRequest(BaseModel):
    session_id: str
    content: str = Field(min_length=1, max_length=4000)


class MessageResponse(BaseModel):
    assistant_message: str
    # Set to true when the agent fired a red-flag interrupt and the server
    # auto-finalized the brief. The client should navigate to brief_url.
    ended: bool = False
    brief_url: Optional[str] = None


class FinalizeRequest(BaseModel):
    session_id: str


class FinalizeResponse(BaseModel):
    call_id: str
    brief_url: str
    summary: str


def _format_transcript(history: list[ChatMessage]) -> str:
    """Render the session as the same Agent:/Patient: format as our test fixtures.
    The extraction prompt and Pydantic schema are agnostic to phrasing, but a
    consistent transcript format keeps debugging diff-friendly."""
    lines: list[str] = []
    for m in history:
        prefix = "Agent" if m.role == "assistant" else "Patient"
        lines.append(f"{prefix}: {m.content.strip()}")
    return "\n".join(lines)


# Detects `flag_red_flag(symptom="...", severity="emergent")` and minor variants.
# This shows up in web-chat replies because we don't register tools with Gemini
# in the chat path (only Vapi does); the model follows the prompt's instruction
# to "call flag_red_flag" and writes it as text. We strip the syntax from the
# spoken reply and capture the structured args as a RedFlag entry.
_TOOL_CALL_LEAK_RE = re.compile(
    r"""
    flag_red_flag                       # function name
    \s*\(\s*                            # opening paren
    symptom\s*=\s*["']([^"']*)["']      # 1: symptom string
    \s*,\s*
    severity\s*=\s*["']?(concern|urgent|emergent)["']?
    (?:\s*,\s*advised_action\s*=\s*["']([^"']*)["'])?  # 3: optional advised_action
    \s*\)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_red_flag_from_reply(reply: str) -> tuple[str, Optional[RedFlag]]:
    """If the agent leaked a `flag_red_flag(...)` call as text, parse it out.

    Returns (cleaned_reply, red_flag_or_none). The cleaned reply has the
    pseudo-tool-call removed and surrounding whitespace tidied. The RedFlag,
    if present, gets recorded on the session and merged into the brief at
    finalize.

    This is a band-aid for the fact that web chat doesn't register tools
    with Gemini. The proper fix is to add tools=[flag_red_flag] to the
    chat_turn call so the model uses Gemini's structured function-calling
    API instead of writing the call inline. Tracked as future work.
    """
    m = _TOOL_CALL_LEAK_RE.search(reply)
    if not m:
        return reply, None

    symptom = (m.group(1) or "").strip()
    severity = (m.group(2) or "concern").strip().lower()
    advised = (m.group(3) or "").strip() or "Hang up and call 911 / go to nearest ER per agent script"

    cleaned = _TOOL_CALL_LEAK_RE.sub("", reply)
    # Tidy up leftover punctuation / whitespace from the substitution.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = cleaned.strip()

    if severity not in ("concern", "urgent", "emergent"):
        severity = "concern"

    try:
        flag = RedFlag(
            symptom=symptom or "(unspecified)",
            severity=severity,  # type: ignore[arg-type]
            advised_action=advised,
        )
    except Exception:
        logger.warning("could not build RedFlag from leaked tool-call: symptom=%r severity=%r", symptom, severity)
        return cleaned, None

    return cleaned, flag


def _build_brief_for_session(sess: Session) -> tuple[str, "ClinicalIntakeBrief", str]:  # type: ignore[name-defined]
    """Run the extraction pipeline on the session and merge live red flags.

    Returns (transcript, brief, brief_url). Caller persists artifacts and
    drops the session. Splitting this out lets `/chat/finalize` and the
    auto-finalize-on-red-flag path share one implementation.
    """
    transcript = _format_transcript(sess.history)
    ended_at = datetime.now(timezone.utc)

    brief = extract_from_transcript(
        transcript,
        call_id=sess.session_id,
        started_at=sess.started_at,
        ended_at=ended_at,
    )

    # Merge any red flags captured live from leaked tool-call syntax in the
    # chat replies. Dedup by lowercase symptom so we don't double-record what
    # the extractor also picked up from the transcript.
    if sess.red_flags:
        seen = {rf.symptom.lower() for rf in brief.red_flags}
        merged = list(brief.red_flags)
        for f in sess.red_flags:
            if f.symptom.lower() not in seen:
                merged.append(f)
                seen.add(f.symptom.lower())
        brief = brief.model_copy(update={"red_flags": merged})

    return transcript, brief, f"/briefs/{sess.session_id}"


def _persist_brief(sess: Session, transcript: str, brief) -> None:  # type: ignore[no-untyped-def]
    """Atomic-write all three artifacts. Caller handles OSError."""
    save_transcript(sess.session_id, transcript)
    save_json(sess.session_id, brief.model_dump(mode="json"))
    save_markdown(sess.session_id, brief.to_markdown())


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/start", response_model=StartResponse)
def start() -> StartResponse:
    """Open a new intake session. Seeds the history with the canned greeting
    so the model treats it as the first turn it 'said'."""
    sess = _store.create()
    sess.history.append(ChatMessage(role="assistant", content=GREETING))
    logger.info("chat session started: %s", sess.session_id)
    return StartResponse(session_id=sess.session_id, first_message=GREETING)


@router.post("/message", response_model=MessageResponse)
def message(req: MessageRequest) -> MessageResponse:
    """Append the patient's turn, generate the agent's reply, return it.

    If the agent's reply contains a leaked `flag_red_flag(...)` tool-call
    (which happens because web chat doesn't register tools with Gemini),
    we strip the syntax from the spoken text, record the red flag on the
    session, and auto-finalize the brief. The response includes
    `ended=True` and `brief_url` so the client can navigate immediately
    instead of leaving the patient staring at an empty chat after the
    redirect script.
    """
    sess = _store.get(req.session_id)
    sess.history.append(ChatMessage(role="user", content=req.content.strip()))

    try:
        reply = chat_turn(sess.history, system=SYSTEM_PROMPT)
    except LLMQuotaExhausted as e:
        sess.history.pop()
        logger.warning("chat quota exhausted for session %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except LLMUpstreamError as e:
        sess.history.pop()
        logger.exception("chat LLM upstream error for session %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except Exception:
        # Anything else: roll back so a retry doesn't double-record the turn,
        # then let FastAPI's 500 path handle it (these are real bugs we want
        # to see in logs, not swallow with a friendly message).
        sess.history.pop()
        raise

    # Strip any leaked tool-call syntax before showing to the patient or
    # writing to the transcript. Capture the structured red flag if present.
    cleaned_reply, red_flag = _extract_red_flag_from_reply(reply)
    sess.history.append(ChatMessage(role="assistant", content=cleaned_reply))

    if red_flag is None:
        return MessageResponse(assistant_message=cleaned_reply)

    # Red flag fired. Record it, auto-finalize, and tell the client to
    # navigate. Even if the patient panics and closes the tab, the brief
    # is already on disk before this response goes out.
    sess.red_flags.append(red_flag)
    logger.info(
        "chat red flag recorded for session %s: severity=%s symptom=%r",
        sess.session_id, red_flag.severity, red_flag.symptom,
    )

    try:
        transcript, brief, brief_url = _build_brief_for_session(sess)
        _persist_brief(sess, transcript, brief)
    except (LLMQuotaExhausted, LLMUpstreamError, ValidationError):
        # Auto-finalize failed. The transcript may still get saved as a
        # courtesy so the recovery CLI can rebuild later. Don't blow up the
        # chat reply — the patient already has the 911 advisory; getting
        # the brief written is a follow-up concern, not blocking.
        logger.exception("auto-finalize on red flag failed for session %s", sess.session_id)
        try:
            save_transcript(sess.session_id, _format_transcript(sess.history))
        except OSError:
            logger.exception("also failed to save transcript for %s", sess.session_id)
        # Return the cleaned reply but mark ended so the UI still closes the call.
        return MessageResponse(assistant_message=cleaned_reply, ended=True, brief_url=None)
    except OSError:
        logger.exception("auto-finalize disk write failed for session %s", sess.session_id)
        return MessageResponse(assistant_message=cleaned_reply, ended=True, brief_url=None)

    _store.drop(sess.session_id)
    return MessageResponse(assistant_message=cleaned_reply, ended=True, brief_url=brief_url)


@router.post("/finalize", response_model=FinalizeResponse)
def finalize(req: FinalizeRequest) -> FinalizeResponse:
    """End the intake: extract the brief, write artifacts to disk, drop the session.

    Session is dropped only on success — a failed finalize leaves the session
    intact so the patient can retry without losing their turns.

    Same code path as the auto-finalize-on-red-flag branch in /chat/message,
    so a brief from a red-flag interrupt looks identical to one from a normal
    "End Intake" click.
    """
    sess = _store.get(req.session_id)
    if not any(m.role == "user" for m in sess.history):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot finalize: no patient turns recorded",
        )

    try:
        transcript, brief, brief_url = _build_brief_for_session(sess)
    except LLMQuotaExhausted as e:
        logger.warning("finalize quota exhausted for session %s", sess.session_id)
        # Persist the transcript anyway — recovery CLI can re-run extraction
        # later (`python -m app.brief briefs/<id>.transcript.txt`) once quota
        # is back. Avoids losing the patient's intake to a transient 429.
        try:
            save_transcript(sess.session_id, _format_transcript(sess.history))
        except OSError:
            logger.exception("also failed to save transcript for %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except LLMUpstreamError as e:
        logger.exception("finalize extraction failed for session %s", sess.session_id)
        try:
            save_transcript(sess.session_id, _format_transcript(sess.history))
        except OSError:
            logger.exception("also failed to save transcript for %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except ValidationError as e:
        logger.exception("brief failed Pydantic validation for session %s", sess.session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="extracted brief failed schema validation",
        ) from e

    try:
        _persist_brief(sess, transcript, brief)
    except OSError as e:
        logger.exception("disk write failed during finalize for %s", sess.session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to persist brief: {e}",
        ) from e

    summary = brief.chief_complaint.summary
    _store.drop(sess.session_id)
    return FinalizeResponse(
        call_id=sess.session_id,
        brief_url=brief_url,
        summary=summary,
    )


__all__ = ["router", "GREETING"]
