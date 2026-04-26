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
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from app.brief import extract_from_transcript
from app.llm import ChatMessage, LLMQuotaExhausted, LLMUpstreamError, chat_turn
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
    """Append the patient's turn, generate the agent's reply, return it."""
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

    sess.history.append(ChatMessage(role="assistant", content=reply))
    return MessageResponse(assistant_message=reply)


@router.post("/finalize", response_model=FinalizeResponse)
def finalize(req: FinalizeRequest) -> FinalizeResponse:
    """End the intake: extract the brief, write artifacts to disk, drop the session.

    Session is dropped only on success — a failed finalize leaves the session
    intact so the patient can retry without losing their turns.
    """
    sess = _store.get(req.session_id)
    if not any(m.role == "user" for m in sess.history):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot finalize: no patient turns recorded",
        )

    transcript = _format_transcript(sess.history)
    ended_at = datetime.now(timezone.utc)

    try:
        brief = extract_from_transcript(
            transcript,
            call_id=sess.session_id,
            started_at=sess.started_at,
            ended_at=ended_at,
        )
    except LLMQuotaExhausted as e:
        logger.warning("finalize quota exhausted for session %s", sess.session_id)
        # Persist the transcript anyway — recovery CLI can re-run extraction
        # later (`python -m app.brief briefs/<id>.transcript.txt`) once quota
        # is back. Avoids losing the patient's intake to a transient 429.
        try:
            save_transcript(sess.session_id, transcript)
        except OSError:
            logger.exception("also failed to save transcript for %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except LLMUpstreamError as e:
        logger.exception("finalize extraction failed for session %s", sess.session_id)
        try:
            save_transcript(sess.session_id, transcript)
        except OSError:
            logger.exception("also failed to save transcript for %s", sess.session_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except ValidationError as e:
        logger.exception("brief failed Pydantic validation for session %s", sess.session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="extracted brief failed schema validation",
        ) from e

    # Persist all three artifacts together. If markdown write fails after
    # JSON succeeded the JSON is still readable — view_brief renders from JSON.
    try:
        save_transcript(sess.session_id, transcript)
        save_json(sess.session_id, brief.model_dump(mode="json"))
        save_markdown(sess.session_id, brief.to_markdown())
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
        brief_url=f"/briefs/{sess.session_id}",
        summary=summary,
    )


__all__ = ["router", "GREETING"]
