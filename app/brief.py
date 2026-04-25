"""Brief extraction — turns a transcript into a ClinicalIntakeBrief.

This module is the *single* extraction path. Both transports (web chat and
Vapi voice) end up here at end-of-intake. We do not depend on Vapi's
structured-output feature; we own the extraction so the same code path is
testable, deterministic-ish, and not coupled to Vapi's webhook payload shape.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

from app.llm import generate_structured
from app.models import (
    CallMetadata,
    ClinicalIntakeBrief,
)
from app.prompts import EXTRACTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def extract_from_transcript(
    transcript: str,
    *,
    call_id: str,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
) -> ClinicalIntakeBrief:
    """Extract a structured clinical brief from a transcript.

    The Gemini call is constrained by the ClinicalIntakeBrief schema, so the
    result already conforms to the JSON shape we want. We re-validate with
    Pydantic both as a belt-and-suspenders check (the SDK's `parsed` may
    silently drop unknown fields) and to attach call_metadata.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        raise ValueError("extract_from_transcript: transcript is empty")

    raw = generate_structured(
        prompt=_format_extraction_prompt(transcript),
        system=EXTRACTION_SYSTEM_PROMPT,
        schema=ClinicalIntakeBrief,
    )

    # call_metadata is informational and may not be in the model's output.
    # Stitch it on after validation so the LLM doesn't have to invent it.
    raw.pop("call_metadata", None)

    try:
        brief = ClinicalIntakeBrief.model_validate(raw)
    except ValidationError as e:
        logger.error(
            "Brief failed Pydantic validation. Raw payload: %s", raw
        )
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
            )
        }
    )
    return brief


def _format_extraction_prompt(transcript: str) -> str:
    """Wrap the transcript with a short instruction so the model knows what to do."""
    return (
        "Below is a transcript of a phone call between a clinical intake "
        "assistant and a patient. Extract the clinical information into the "
        "structured brief format defined by the response schema. Follow the "
        "rules in the system instruction strictly.\n\n"
        "<transcript>\n"
        f"{transcript}\n"
        "</transcript>"
    )


__all__ = ["extract_from_transcript"]


# Convenience for ad-hoc inspection: `python -m app.brief <path>`.
if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    if len(sys.argv) != 2:
        print("usage: python -m app.brief <transcript_file>", file=sys.stderr)
        sys.exit(2)
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    brief = extract_from_transcript(
        text,
        call_id="adhoc",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    print(brief.to_markdown())
