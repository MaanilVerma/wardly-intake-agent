"""Pydantic v2 models for the clinical intake brief.

These mirror prompts/brief_schema.json. If you change the schema,
update both files together.
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ---------- leaf models ----------

class ChiefComplaint(BaseModel):
    verbatim: str = Field(..., description="Patient's own words. Quote them.")
    summary: str = Field(..., description="Clinician-shorthand summary.")


class HPI(BaseModel):
    onset: Optional[str] = None
    location: Optional[str] = None
    duration: Optional[str] = None
    character: Optional[str] = None
    aggravating_factors: list[str] = Field(default_factory=list)
    alleviating_factors: list[str] = Field(default_factory=list)
    radiation: Optional[str] = None
    severity_now: Optional[int] = Field(None, ge=0, le=10)
    severity_worst: Optional[int] = Field(None, ge=0, le=10)
    timing: Optional[str] = None
    associated_symptoms: list[str] = Field(default_factory=list)
    context: Optional[str] = None
    prior_episodes: Optional[str] = None
    treatments_tried: list[str] = Field(default_factory=list)
    narrative: str = Field(..., description="2-4 sentence clinician-style summary.")


class ROSSystem(BaseModel):
    positives: list[str] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.positives and not self.negatives


class ROS(BaseModel):
    constitutional: Optional[ROSSystem] = None
    heent: Optional[ROSSystem] = None
    cardiac: Optional[ROSSystem] = None
    respiratory: Optional[ROSSystem] = None
    gi: Optional[ROSSystem] = None
    gu: Optional[ROSSystem] = None
    msk: Optional[ROSSystem] = None
    skin: Optional[ROSSystem] = None
    neuro: Optional[ROSSystem] = None
    psych: Optional[ROSSystem] = None
    endocrine: Optional[ROSSystem] = None
    heme_lymph: Optional[ROSSystem] = None
    allergy_immuno: Optional[ROSSystem] = None

    def asked_systems(self) -> list[tuple[str, ROSSystem]]:
        """Return (label, system) for systems that have at least one entry."""
        labels = {
            "constitutional": "Constitutional",
            "heent": "HEENT",
            "cardiac": "Cardiac",
            "respiratory": "Respiratory",
            "gi": "GI",
            "gu": "GU",
            "msk": "Musculoskeletal",
            "skin": "Skin",
            "neuro": "Neurologic",
            "psych": "Psychiatric",
            "endocrine": "Endocrine",
            "heme_lymph": "Heme/Lymph",
            "allergy_immuno": "Allergy/Immuno",
        }
        out: list[tuple[str, ROSSystem]] = []
        for key, label in labels.items():
            sys: Optional[ROSSystem] = getattr(self, key)
            if sys is not None and not sys.is_empty():
                out.append((label, sys))
        return out


class RedFlag(BaseModel):
    symptom: str
    severity: Literal["concern", "urgent", "emergent"]
    advised_action: Optional[str] = None


class PatientDemographics(BaseModel):
    age: Optional[int] = Field(None, ge=0, le=120)
    sex: Optional[Literal["male", "female", "other", "prefer_not_to_say"]] = None
    pregnancy_status: Optional[Literal["pregnant", "not_pregnant", "unknown", "n/a"]] = None


class CallMetadata(BaseModel):
    call_id: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_sec: Optional[int] = None


# ---------- patient header band + history (extension fields) ----------

class PatientIdentifiers(BaseModel):
    """Captured in the Phase 0 verification step at the top of the call —
    name, appointment time, visit type. The line a clinician scans first."""
    name: Optional[str] = None
    date_of_birth: Optional[str] = None
    preferred_pronouns: Optional[str] = None
    appointment_time: Optional[str] = None
    appointment_time_iso: Optional[datetime] = None  # resolved from verbatim using today's date
    visit_type: Optional[Literal["new_patient", "follow_up", "urgent", "telehealth", "unknown"]] = None
    reason_for_visit_today: Optional[str] = None


class Medication(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None


class Allergy(BaseModel):
    substance: str
    reaction: Optional[str] = None


class SocialHistory(BaseModel):
    smoking_status: Optional[Literal["never", "former", "current", "unknown"]] = None
    alcohol_use: Optional[str] = None
    recreational_drugs: Optional[str] = None
    occupation: Optional[str] = None

    def is_empty(self) -> bool:
        return all(v is None for v in (
            self.smoking_status, self.alcohol_use, self.recreational_drugs, self.occupation,
        ))


class ClinicianNotes(BaseModel):
    """AI-generated, hedged. NOT a diagnosis. Surfaced in a clearly-labeled
    panel at the bottom of the brief."""
    differential_considerations: list[str] = Field(default_factory=list)
    suggested_followups: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.differential_considerations and not self.suggested_followups


class ICD10Candidate(BaseModel):
    code: str
    description: str


class Completeness(BaseModel):
    """Computed in Python from the populated fields — never trusted from the LLM.
    Surfaced as a chip in the brief footer to give the clinician a quick read on
    how thorough the intake was."""
    score: int = Field(ge=0, le=100)
    missing_fields: list[str] = Field(default_factory=list)


# ---------- top-level model ----------

class ClinicalIntakeBrief(BaseModel):
    chief_complaint: ChiefComplaint
    hpi: HPI
    ros: ROS
    red_flags: list[RedFlag] = Field(default_factory=list)
    patient_demographics: Optional[PatientDemographics] = None
    patient_identifiers: Optional[PatientIdentifiers] = None
    past_medical_history: list[str] = Field(default_factory=list)
    current_medications: list[Medication] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    social_history: Optional[SocialHistory] = None
    clinician_notes: Optional[ClinicianNotes] = None
    icd10_candidates: list[ICD10Candidate] = Field(default_factory=list)
    completeness: Optional[Completeness] = None
    intake_notes: Optional[str] = None
    call_metadata: Optional[CallMetadata] = None

    @field_validator("hpi")
    @classmethod
    def severity_now_le_worst(cls, v: HPI) -> HPI:
        if v.severity_now is not None and v.severity_worst is not None:
            if v.severity_now > v.severity_worst:
                # Noisy LLM output — normalize rather than reject, but log it
                # so a clinician auditing the brief can see we touched the data.
                logger.warning(
                    "severity_now (%d) > severity_worst (%d); raising worst to match",
                    v.severity_now, v.severity_worst,
                )
                v.severity_worst = v.severity_now
        return v

    def to_markdown(self) -> str:
        """Render the brief as the Markdown a clinician will read.

        The format is intentionally close to standard chart-note shorthand.
        Empty fields are omitted gracefully so the brief never has hollow
        sections.
        """
        parts: list[str] = []

        # Header
        parts.append("# Clinical Intake Brief\n")
        if self.call_metadata:
            meta = self.call_metadata
            meta_line_parts = [f"**Call ID**: `{meta.call_id}`"]
            if meta.started_at:
                meta_line_parts.append(f"**Date**: {meta.started_at.isoformat()}")
            if meta.duration_sec:
                meta_line_parts.append(f"**Duration**: {meta.duration_sec}s")
            parts.append("    ".join(meta_line_parts))
            parts.append("")

        # Red flags FIRST if present — clinician must see these immediately
        if self.red_flags:
            parts.append("## Red Flags\n")
            for rf in self.red_flags:
                parts.append(f"- **[{rf.severity.upper()}]** {rf.symptom}")
                if rf.advised_action:
                    parts.append(f"  - *Advised*: {rf.advised_action}")
            parts.append("")

        # Demographics (one line if present)
        if self.patient_demographics:
            d = self.patient_demographics
            demo_bits = []
            if d.age is not None:
                demo_bits.append(f"{d.age}yo")
            if d.sex:
                demo_bits.append(d.sex)
            if d.pregnancy_status and d.pregnancy_status != "n/a":
                demo_bits.append(f"pregnancy: {d.pregnancy_status}")
            if demo_bits:
                parts.append(f"**Patient**: {', '.join(demo_bits)}\n")

        # CC
        cc = self.chief_complaint
        parts.append("## Chief Complaint\n")
        parts.append(f"> \"{cc.verbatim}\"\n")
        parts.append(f"_{cc.summary}_\n")

        # HPI
        hpi = self.hpi
        parts.append("## History of Present Illness\n")
        parts.append(hpi.narrative + "\n")

        hpi_lines: list[str] = []
        if hpi.onset:           hpi_lines.append(f"- **Onset**: {hpi.onset}")
        if hpi.location:        hpi_lines.append(f"- **Location**: {hpi.location}")
        if hpi.character:       hpi_lines.append(f"- **Quality**: {hpi.character}")
        if hpi.radiation:       hpi_lines.append(f"- **Radiation**: {hpi.radiation}")
        sev_bits: list[str] = []
        if hpi.severity_now is not None:   sev_bits.append(f"{hpi.severity_now}/10 now")
        if hpi.severity_worst is not None: sev_bits.append(f"{hpi.severity_worst}/10 worst")
        if sev_bits:            hpi_lines.append(f"- **Severity**: {'; '.join(sev_bits)}")
        if hpi.timing:          hpi_lines.append(f"- **Timing**: {hpi.timing}")
        if hpi.duration:        hpi_lines.append(f"- **Duration**: {hpi.duration}")
        if hpi.aggravating_factors:
            hpi_lines.append(f"- **Worse with**: {', '.join(hpi.aggravating_factors)}")
        if hpi.alleviating_factors:
            hpi_lines.append(f"- **Better with**: {', '.join(hpi.alleviating_factors)}")
        if hpi.associated_symptoms:
            hpi_lines.append(f"- **Associated**: {', '.join(hpi.associated_symptoms)}")
        if hpi.treatments_tried:
            hpi_lines.append(f"- **Treatments tried**: {', '.join(hpi.treatments_tried)}")
        if hpi.prior_episodes:
            hpi_lines.append(f"- **Prior episodes**: {hpi.prior_episodes}")
        if hpi.context:
            hpi_lines.append(f"- **Context**: {hpi.context}")
        if hpi_lines:
            parts.extend(hpi_lines)
            parts.append("")

        # ROS — only systems with entries
        asked = self.ros.asked_systems()
        if asked:
            parts.append("## Review of Systems\n")
            for label, sys in asked:
                pos = "; ".join(sys.positives) if sys.positives else "—"
                neg = "; ".join(sys.negatives) if sys.negatives else "—"
                parts.append(f"- **{label}**  Pos: {pos}  ·  Neg: {neg}")
            parts.append("")

        # Notes
        if self.intake_notes:
            parts.append("## Intake Notes\n")
            parts.append(self.intake_notes + "\n")

        return "\n".join(parts).rstrip() + "\n"


# ---------- completeness ----------
#
# Computed in code from the populated fields, never trusted from the LLM.
# We intentionally weight the *spine* of a clinical intake (CC verbatim, HPI
# narrative, two HPI specifics, ROS coverage, red-flag awareness) higher than
# nice-to-haves like medications. The score is a clinician-facing read on
# how thorough the intake was — not a precision metric.

# Each entry: (key shown in `missing_fields`, weight, callable that returns True
# when the field is "captured"). Weights sum to 100 for a clean percentage.
_COMPLETENESS_CHECKS: list[tuple[str, int, str]] = [
    # CC & HPI core (40 pts)
    ("chief_complaint.verbatim", 10, "cc_verbatim"),
    ("chief_complaint.summary", 5, "cc_summary"),
    ("hpi.narrative", 10, "hpi_narrative"),
    ("hpi.onset", 4, "hpi_onset"),
    ("hpi.character", 3, "hpi_character"),
    ("hpi.severity", 4, "hpi_severity"),
    ("hpi.timing", 4, "hpi_timing"),
    # ROS (20 pts) — at least 2 systems with content, constitutional present
    ("ros.constitutional", 6, "ros_constitutional"),
    ("ros.coverage", 14, "ros_coverage"),
    # Patient / visit context (15 pts)
    ("patient_identifiers.name", 6, "name"),
    ("patient_identifiers.appointment_time", 4, "appointment_time"),
    ("patient_identifiers.visit_type", 5, "visit_type"),
    # Quick history (15 pts)
    ("past_medical_history", 5, "pmh"),
    ("current_medications", 5, "meds"),
    ("allergies", 5, "allergies"),
    # AI-assisted niceties (10 pts)
    ("clinician_notes", 5, "clinician_notes"),
    ("icd10_candidates", 5, "icd10"),
]


def compute_completeness(brief: ClinicalIntakeBrief) -> Completeness:
    """Score the brief 0-100 against the canonical intake spine.

    The score is for the clinician's eye, not a precision metric. Anything the
    LLM puts in `brief.completeness` is ignored — the caller is expected to
    overwrite it with the result of this function.
    """
    cc = brief.chief_complaint
    hpi = brief.hpi
    ros = brief.ros
    pid = brief.patient_identifiers

    presence: dict[str, bool] = {
        "cc_verbatim":       bool(cc.verbatim and cc.verbatim.strip()),
        "cc_summary":        bool(cc.summary and cc.summary.strip()),
        "hpi_narrative":     bool(hpi.narrative and len(hpi.narrative.strip()) >= 30),
        "hpi_onset":         bool(hpi.onset),
        "hpi_character":     bool(hpi.character),
        "hpi_severity":      hpi.severity_now is not None or hpi.severity_worst is not None,
        "hpi_timing":        bool(hpi.timing),
        "ros_constitutional": ros.constitutional is not None and not ros.constitutional.is_empty(),
        "ros_coverage":      len(ros.asked_systems()) >= 2,
        "name":              bool(pid and pid.name),
        "appointment_time":  bool(pid and pid.appointment_time),
        "visit_type":        bool(pid and pid.visit_type and pid.visit_type != "unknown"),
        "pmh":               bool(brief.past_medical_history),
        "meds":              bool(brief.current_medications),
        "allergies":         bool(brief.allergies),
        "clinician_notes":   brief.clinician_notes is not None and not brief.clinician_notes.is_empty(),
        "icd10":             bool(brief.icd10_candidates),
    }

    score = 0
    missing: list[str] = []
    for label, weight, key in _COMPLETENESS_CHECKS:
        if presence.get(key):
            score += weight
        else:
            missing.append(label)
    score = max(0, min(100, score))
    return Completeness(score=score, missing_fields=missing)
