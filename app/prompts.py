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

# Faithfulness rules — follow these strictly, the brief lands in a chart

- Extract only information explicitly present in the transcript. Do not \
infer, summarize beyond what was said, or invent.
- chief_complaint.verbatim MUST quote the patient's exact words and MUST be \
a substring of the transcript. Do not paraphrase.
- chief_complaint.summary is a 5-10 word clinician-shorthand summary \
(e.g. "RLQ abdominal pain x 2 days, sharp").
- hpi.narrative is a 2-4 sentence clinician-style paragraph in the third \
person, suitable as the first thing a clinician reads. \
**Never use bracketed placeholders** like [AGE], [SEX], [NAME], [ONSET]. If \
a value is not in the transcript, omit it or rephrase: write "the patient" \
instead of "[AGE]-year-old female", or just "a female patient" if age is \
unknown. The narrative must read as finished prose, not a fill-in-the-blank \
template.
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

# Patient identifiers + visit context

- patient_identifiers.name: verbatim, as the patient stated it.
- patient_identifiers.appointment_time: VERBATIM — the patient's own phrasing \
(e.g. "tomorrow at 2pm", "next Tuesday morning"). Preserve how they said it.
- patient_identifiers.appointment_time_iso: ISO 8601 datetime resolved from \
that verbatim phrase using "Today's date" provided in the user message as the \
anchor. Examples (assume today is 2026-04-26 Sunday):
    - "tomorrow at 2pm"        → 2026-04-27T14:00:00
    - "next Tuesday morning"   → 2026-04-28T09:00:00 (default morning to 9am)
    - "this Friday at noon"    → 2026-05-01T12:00:00
    - "April 30 at 11"         → 2026-04-30T11:00:00
  Default ambiguous time-of-day to 09:00 (morning), 14:00 (afternoon), 19:00 \
(evening). If the date itself is ambiguous ("sometime next week"), leave \
appointment_time_iso null.
- patient_identifiers.visit_type: one of new_patient | follow_up | urgent | \
telehealth | unknown. Use "unknown" if the patient didn't clearly say.

# Quick history (PMH / meds / allergies / social)

- past_medical_history: each entry is a single named chronic condition \
(e.g. "hypertension", "type 2 diabetes", "asthma"). Do NOT include \
generic words like "drug allergies", "allergies", "medications", "healthy", \
or "no conditions" — those are not PMH entries. Drug allergies belong only \
in the `allergies` field.
- current_medications: each entry has at minimum `name`. Include `dose` and \
`frequency` only if the patient stated them. "Multivitamin" with no dose is \
fine.
- allergies: each entry has at minimum `substance`. If the patient said \
"no allergies" or "NKDA", emit an empty array — that's a meaningful zero.
- social_history: only fields the patient disclosed. Skip the rest.

# Post-hoc clinician aids — clearly hedged, NOT diagnoses

- clinician_notes.differential_considerations: up to 4 short hedged lines, \
each ≤ 140 chars. Frame as considerations, not conclusions ("consider X vs Y \
given Z"). Skip if the data is too sparse to support any reasonable \
consideration.
- clinician_notes.suggested_followups: up to 2 follow-up questions a \
clinician might want to ask in person, given gaps in the intake.
- icd10_candidates: 1-3 candidate ICD-10 codes for the chief complaint, \
each as {code, description}. Use commonly-known codes (e.g. R10.31 for RLQ \
abdominal pain). If you're unsure, emit fewer codes — better to be sparse \
than to fabricate.

# Things the LLM must NOT populate

- completeness: leave this field unset — the backend computes it deterministically.
- call_metadata: leave this field unset — the backend stitches it on after.
"""

__all__ = [
    "SYSTEM_PROMPT",
    "BRIEF_SCHEMA",
    "EXTRACTION_SYSTEM_PROMPT",
    "SYSTEM_PROMPT_PATH",
    "BRIEF_SCHEMA_PATH",
]
