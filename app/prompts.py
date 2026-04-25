"""Authored content loaded from disk at import time.

The clinical intake system prompt and the brief JSON Schema are *authored*
content — they encode the conversation design and the structured-output
contract. Treat them as first-class inputs, not strings buried in code.

Loading at import time means a syntax error in the prompt or schema fails
fast at server startup rather than at the first patient call.
"""

from __future__ import annotations
import json
from pathlib import Path

# Paths are resolved relative to the repo root (the parent of app/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _REPO_ROOT / "prompts"

SYSTEM_PROMPT_PATH = _PROMPTS_DIR / "system_prompt.md"
BRIEF_SCHEMA_PATH = _PROMPTS_DIR / "brief_schema.json"


def _load_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"required prompt asset missing: {path}")
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"required schema asset missing: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# Conversation system prompt — loaded into Vapi's model.systemPrompt and into
# the web chat's per-turn call. Same text drives both transports.
SYSTEM_PROMPT: str = _load_text(SYSTEM_PROMPT_PATH)

# JSON Schema for the structured brief — informational; the actual extraction
# uses the Pydantic model (app.models.ClinicalIntakeBrief) which is the
# canonical, $ref-resolved equivalent of this file.
BRIEF_SCHEMA: dict = _load_json(BRIEF_SCHEMA_PATH)


# Post-hoc extraction prompt for the medical scribe. This is *different* from
# the live-conversation prompt above — its job is to read a finished
# transcript and emit a faithful structured brief, not to talk to a patient.
EXTRACTION_SYSTEM_PROMPT: str = """\
You are a medical scribe. You will be given a transcript of a phone call \
between a clinical intake assistant and a patient. Extract the clinical \
information from the transcript into the structured brief format defined by \
the response schema.

Rules — follow these strictly, the brief lands in a clinical chart:

- Extract only information explicitly present in the transcript. Do not \
infer, summarize beyond what was said, or invent.
- chief_complaint.verbatim MUST quote the patient's exact words and MUST be \
a substring of the transcript. Do not paraphrase.
- chief_complaint.summary is a 5-10 word clinician-shorthand summary \
(e.g. "RLQ abdominal pain x 2 days, sharp").
- hpi.narrative is a 2-4 sentence clinician-style paragraph in the third \
person, suitable as the first thing a clinician reads.
- For ROS: capture both pertinent positives (symptoms the patient confirmed) \
and pertinent negatives (symptoms explicitly denied). Only include systems \
that were actually asked about. A patient saying "no fever" in response to \
a question is a pertinent negative; if fever wasn't discussed, leave the \
constitutional system out entirely.
- severity_now and severity_worst are integers 0-10. severity_worst must \
be >= severity_now.
- If a field was not discussed, leave it null (or omit it for arrays). \
Don't guess.
- red_flags: include only symptoms that were treated as emergencies during \
the call — look for the agent telling the patient to call 911, go to the ER, \
or seek emergency care.
"""

__all__ = [
    "SYSTEM_PROMPT",
    "BRIEF_SCHEMA",
    "EXTRACTION_SYSTEM_PROMPT",
    "SYSTEM_PROMPT_PATH",
    "BRIEF_SCHEMA_PATH",
]
