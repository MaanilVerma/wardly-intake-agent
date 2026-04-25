"""Pydantic v2 models for the clinical intake brief.

These mirror prompts/brief_schema.json. If you change the schema,
update both files together.
"""

from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


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


# ---------- top-level model ----------

class ClinicalIntakeBrief(BaseModel):
    chief_complaint: ChiefComplaint
    hpi: HPI
    ros: ROS
    red_flags: list[RedFlag] = Field(default_factory=list)
    patient_demographics: Optional[PatientDemographics] = None
    intake_notes: Optional[str] = None
    call_metadata: Optional[CallMetadata] = None

    @field_validator("hpi")
    @classmethod
    def severity_now_le_worst(cls, v: HPI) -> HPI:
        if v.severity_now is not None and v.severity_worst is not None:
            if v.severity_now > v.severity_worst:
                # don't raise — clinical data has noise; just normalize
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
            parts.append("## ⚠️ Red Flags\n")
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
