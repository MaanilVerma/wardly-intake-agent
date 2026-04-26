"""Brief extraction — turns a transcript into a ClinicalIntakeBrief.

This module is the *single* extraction path. Both transports (web chat and
Vapi voice) end up here at end-of-intake. We do not depend on Vapi's
structured-output feature; we own the extraction so the same code path is
testable, deterministic-ish, and not coupled to Vapi's webhook payload shape.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

from app.llm import generate_structured
from app.models import (
    CallMetadata,
    ClinicalIntakeBrief,
    compute_completeness,
)
from app.prompts import EXTRACTION_SYSTEM_PROMPT


def _strip_placeholders(text: str | None) -> str | None:
    """Strip [ALL_CAPS] placeholders the LLM sometimes leaks into the HPI
    ('a [AGE]-year-old female ...') and tidy the surrounding punctuation.

    The extraction prompt forbids these — this is the safety net for when
    the model ignores it.
    """
    if not text:
        return text
    # Common multi-token templates first so we get rid of them whole.
    cleaned = re.sub(r"\b(an?|the)\s+\[[A-Z_ ]+\]\s*-\s*year[-\s]?old\b", r"\1", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[[A-Z_ ]+\]\s*-\s*year[-\s]?old", "", cleaned)
    # Then any leftover bracketed CAPS token.
    cleaned = re.sub(r"\[[A-Z][A-Z_ ]*\]", "", cleaned)
    # Collapse whitespace + dangling punctuation.
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:])\s*\1+", r"\1", cleaned)  # ", ," → ","
    cleaned = re.sub(r"\bwith\s*\.", "with no further detail.", cleaned)
    return cleaned.strip()

logger = logging.getLogger(__name__)


def extract_from_transcript(
    transcript: str,
    *,
    call_id: str,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
) -> ClinicalIntakeBrief:
    """Extract a structured clinical brief from a transcript.

    The Gemini call is schema-constrained to ClinicalIntakeBrief, but we
    still re-validate with Pydantic — the SDK's `parsed` can silently drop
    unknown fields, and we always need to stitch on call_metadata after.

    `started_at` (or wall-clock now) is passed to the LLM as today's-date
    context so it can resolve relative phrases like "tomorrow at 2pm" into
    `appointment_time_iso`.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        raise ValueError("extract_from_transcript: transcript is empty")

    today = (started_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

    raw = generate_structured(
        prompt=_format_extraction_prompt(transcript, today),
        system=EXTRACTION_SYSTEM_PROMPT,
        schema=ClinicalIntakeBrief,
    )

    # call_metadata is informational; stitch on after validation. completeness is
    # computed in code (compute_completeness) and is never trusted from the LLM.
    raw.pop("call_metadata", None)
    raw.pop("completeness", None)

    # Strip any bracketed placeholders Gemini leaked into the narrative.
    if isinstance(raw.get("hpi"), dict):
        raw["hpi"]["narrative"] = _strip_placeholders(raw["hpi"].get("narrative"))

    try:
        brief = ClinicalIntakeBrief.model_validate(raw)
    except ValidationError:
        logger.error("Brief failed Pydantic validation. Raw payload: %s", raw)
        raise

    duration: Optional[int] = None
    if started_at and ended_at:
        duration = max(0, int((ended_at - started_at).total_seconds()))

    brief = brief.model_copy(
        update={
            "call_metadata": CallMetadata(
                call_id=call_id,
                started_at=started_at,
                ended_at=ended_at,
                duration_sec=duration,
            ),
            "completeness": compute_completeness(brief),
        }
    )
    return brief


def _format_extraction_prompt(transcript: str, today: datetime) -> str:
    """Wrap the transcript with the date anchor + short instruction.

    The "Today is …" line is what lets the LLM resolve relative phrases
    like "tomorrow at 2pm" into a concrete ISO datetime (`appointment_time_iso`).
    """
    today_label = today.strftime("%Y-%m-%d (%A)")
    return (
        f"Today's date is {today_label}. Use this as the anchor when resolving "
        "any relative date phrases the patient uses (e.g. 'tomorrow', 'next "
        "Monday', 'this Friday') into the appointment_time_iso field.\n\n"
        "Below is a transcript of a phone call between a clinical intake "
        "assistant and a patient. Extract the clinical information into the "
        "structured brief format defined by the response schema. Follow the "
        "rules in the system instruction strictly.\n\n"
        "<transcript>\n"
        f"{transcript}\n"
        "</transcript>"
    )


__all__ = ["extract_from_transcript"]


# Recovery / ad-hoc CLI:
#   python -m app.brief briefs/<call_id>.transcript.txt
# Re-runs extraction on a saved transcript and writes <call_id>.json + .md
# next to it. Use this to recover a brief whose original background task
# died (e.g. quota exhaustion) — once quota is back, re-run this and the
# brief appears at /briefs/<call_id> like normal.
if __name__ == "__main__":  # pragma: no cover
    import logging as _logging
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv()

    from app.storage import save_json, save_markdown

    _logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if len(sys.argv) != 2:
        print("usage: python -m app.brief <briefs/{call_id}.transcript.txt>", file=sys.stderr)
        sys.exit(2)

    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        sys.exit(2)

    # Derive the call_id from the filename ("<call_id>.transcript.txt").
    name = path.name
    if not name.endswith(".transcript.txt"):
        print(f"file must be named <call_id>.transcript.txt; got {name}", file=sys.stderr)
        sys.exit(2)
    call_id = name[: -len(".transcript.txt")]

    text = path.read_text(encoding="utf-8")
    started_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    ended_at = started_at  # we don't know the actual duration on recovery

    brief = extract_from_transcript(text, call_id=call_id, started_at=started_at, ended_at=ended_at)
    save_json(call_id, brief.model_dump(mode="json"))
    save_markdown(call_id, brief.to_markdown())
    print(f"wrote briefs/{call_id}.json and briefs/{call_id}.md (completeness={brief.completeness.score if brief.completeness else '?'})")
