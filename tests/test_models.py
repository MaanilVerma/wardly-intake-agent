"""Unit tests for the brief models — render correctness and invariants.

These tests do not call the LLM. They exercise the things a clinician sees:
markdown rendering on sparse and dense input, severity sanity, and the
ROS gating that hides systems we never asked about.
"""

from __future__ import annotations
from datetime import datetime, timezone

import pytest

from app.models import (
    Allergy,
    CallMetadata,
    ChiefComplaint,
    ClinicianNotes,
    ClinicalIntakeBrief,
    Completeness,
    HPI,
    ICD10Candidate,
    Medication,
    PatientDemographics,
    PatientIdentifiers,
    RedFlag,
    ROS,
    ROSSystem,
    SocialHistory,
    compute_completeness,
)


def _minimal_brief(**overrides) -> ClinicalIntakeBrief:
    """Construct the smallest valid brief for negative-case tests."""
    base = dict(
        chief_complaint=ChiefComplaint(verbatim="my belly hurts", summary="abdominal pain"),
        hpi=HPI(narrative="Patient reports vague abdominal pain. No further detail captured."),
        ros=ROS(),
    )
    base.update(overrides)
    return ClinicalIntakeBrief(**base)


def test_minimal_brief_renders_without_empty_sections():
    """A bare brief should not show ROS / Red Flags / Notes headers when there's
    nothing to put under them — clinicians scan, hollow sections waste their time."""
    md = _minimal_brief().to_markdown()
    assert "## Chief Complaint" in md
    assert "## History of Present Illness" in md
    assert "## Review of Systems" not in md
    assert "Red Flags" not in md
    assert "Intake Notes" not in md


def test_full_brief_renders_all_sections():
    """A populated brief should expose every section and the structured HPI list."""
    brief = ClinicalIntakeBrief(
        chief_complaint=ChiefComplaint(
            verbatim="this stabbing pain in my right side for two days",
            summary="RLQ abdominal pain x 2 days, sharp",
        ),
        hpi=HPI(
            onset="2 days ago, Tuesday morning",
            location="right lower quadrant",
            character="sharp, stabbing",
            radiation="occasional, to back",
            severity_now=4,
            severity_worst=7,
            timing="intermittent, worse post-prandial",
            aggravating_factors=["heavy meals", "palpation"],
            alleviating_factors=["lying still"],
            associated_symptoms=["nausea"],
            treatments_tried=["ibuprofen 400mg, no relief"],
            prior_episodes="first occurrence",
            context="no preceding injury or dietary change",
            narrative="Patient is a 47yo F with 2 days of intermittent sharp RLQ pain, currently 4/10 (worst 7/10).",
        ),
        ros=ROS(
            constitutional=ROSSystem(positives=["mild fatigue"], negatives=["no fever", "no chills"]),
            gi=ROSSystem(positives=["nausea"], negatives=["no vomiting", "no diarrhea"]),
        ),
        red_flags=[],
        patient_demographics=PatientDemographics(age=47, sex="female"),
        intake_notes="Travel to Mexico 3 weeks ago.",
        call_metadata=CallMetadata(
            call_id="abc-123",
            started_at=datetime(2026, 4, 23, 14, 22, tzinfo=timezone.utc),
            ended_at=datetime(2026, 4, 23, 14, 28, 27, tzinfo=timezone.utc),
            duration_sec=387,
        ),
    )
    md = brief.to_markdown()
    assert "## Chief Complaint" in md
    assert "## History of Present Illness" in md
    assert "## Review of Systems" in md
    assert "**Constitutional**" in md
    assert "**GI**" in md
    assert "## Intake Notes" in md
    assert "47yo" in md
    assert "female" in md
    # Severity is one of the most clinically scanned numbers — rendered both forms.
    assert "4/10 now" in md
    assert "7/10 worst" in md


def test_red_flags_render_first_with_severity_tag():
    """Red flags must appear above the CC so the clinician sees them on first scroll."""
    brief = _minimal_brief(
        red_flags=[
            RedFlag(
                symptom="chest pain radiating to left arm with diaphoresis",
                severity="emergent",
                advised_action="Call 911",
            )
        ]
    )
    md = brief.to_markdown()
    rf_idx = md.index("Red Flags")
    cc_idx = md.index("Chief Complaint")
    assert rf_idx < cc_idx, "Red Flags must render before Chief Complaint"
    assert "[EMERGENT]" in md
    assert "Call 911" in md


def test_severity_now_normalized_when_exceeds_worst():
    """Clinical data is noisy. If severity_now > severity_worst, normalize rather
    than reject — the validator rewrites worst upward so the brief still renders."""
    brief = _minimal_brief(
        hpi=HPI(narrative="Vague pain.", severity_now=8, severity_worst=5)
    )
    assert brief.hpi.severity_now == 8
    assert brief.hpi.severity_worst == 8


def test_ros_only_renders_systems_with_entries():
    """We capture pertinent positives and negatives. Systems we never asked about
    must not appear — that distinction matters clinically."""
    brief = _minimal_brief(
        ros=ROS(
            constitutional=ROSSystem(negatives=["no fever"]),
            gi=ROSSystem(),  # asked-but-empty form: do not render
            cardiac=None,    # never asked: do not render
        )
    )
    md = brief.to_markdown()
    assert "**Constitutional**" in md
    assert "**GI**" not in md
    assert "**Cardiac**" not in md


def test_brief_round_trips_through_json():
    """Serialize and validate back — should be lossless on the data we care about."""
    original = _minimal_brief(
        red_flags=[RedFlag(symptom="severe headache", severity="urgent")],
    )
    payload = original.model_dump_json()
    restored = ClinicalIntakeBrief.model_validate_json(payload)
    assert restored.red_flags[0].symptom == "severe headache"
    assert restored.red_flags[0].severity == "urgent"
    assert restored.chief_complaint.verbatim == original.chief_complaint.verbatim


def test_chief_complaint_required_fields_enforced():
    """CC is the load-bearing field — missing verbatim or summary should fail."""
    with pytest.raises(Exception):
        ChiefComplaint(verbatim="my belly hurts")  # type: ignore[call-arg]
    with pytest.raises(Exception):
        ChiefComplaint(summary="abdominal pain")  # type: ignore[call-arg]


def test_severity_out_of_range_rejected():
    """0-10 is a clinical convention. 11/10 should not pass."""
    with pytest.raises(Exception):
        HPI(narrative="x", severity_now=11)
    with pytest.raises(Exception):
        HPI(narrative="x", severity_worst=-1)


# ---------- Phase 4 extensions ----------

def test_completeness_minimal_brief_gives_low_score():
    """A bare CC + 1-line HPI should not score high — too many gaps."""
    brief = _minimal_brief()
    c = compute_completeness(brief)
    # CC verbatim + summary + (a too-short narrative) = 15 pts at most.
    assert c.score <= 25
    assert "patient_identifiers.name" in c.missing_fields
    assert "ros.coverage" in c.missing_fields


def test_completeness_full_brief_scores_high():
    """A populated brief covering CC/HPI/ROS/Patient/PMH/Meds/Allergies/AI should be ≥80%."""
    brief = ClinicalIntakeBrief(
        chief_complaint=ChiefComplaint(verbatim="my belly hurts", summary="RLQ pain x 2 days"),
        hpi=HPI(
            narrative="Patient is a 47yo F reporting 2 days of RLQ pain, 7/10 worst, intermittent post-prandial.",
            onset="2 days ago",
            character="sharp",
            severity_now=4,
            severity_worst=7,
            timing="intermittent",
        ),
        ros=ROS(
            constitutional=ROSSystem(negatives=["no fever"]),
            gi=ROSSystem(positives=["nausea"]),
        ),
        patient_identifiers=PatientIdentifiers(
            name="Sarah Chen",
            appointment_time="tomorrow at 2pm",
            visit_type="follow_up",
        ),
        past_medical_history=["hypertension"],
        current_medications=[Medication(name="lisinopril", dose="10mg", frequency="daily")],
        allergies=[Allergy(substance="penicillin", reaction="rash")],
        clinician_notes=ClinicianNotes(differential_considerations=["consider appendicitis"]),
        icd10_candidates=[ICD10Candidate(code="R10.31", description="RLQ abdominal pain")],
    )
    c = compute_completeness(brief)
    assert c.score >= 80, f"expected ≥80, got {c.score}, missing={c.missing_fields}"


def test_completeness_score_capped_at_100():
    """Defensive: even if every weight contributes, score ≤ 100."""
    brief = ClinicalIntakeBrief(
        chief_complaint=ChiefComplaint(verbatim="x", summary="y"),
        hpi=HPI(narrative="A long enough HPI narrative to satisfy the 30-char threshold for completeness."),
        ros=ROS(),
    )
    c = compute_completeness(brief)
    assert 0 <= c.score <= 100


def test_completeness_visit_type_unknown_does_not_count():
    """visit_type='unknown' is the explicit fallback — shouldn't credit completeness."""
    brief = _minimal_brief(
        patient_identifiers=PatientIdentifiers(name="X", visit_type="unknown")
    )
    c = compute_completeness(brief)
    assert "patient_identifiers.visit_type" in c.missing_fields


def test_clinician_notes_is_empty_helper():
    assert ClinicianNotes().is_empty()
    assert not ClinicianNotes(differential_considerations=["consider X"]).is_empty()
    assert not ClinicianNotes(suggested_followups=["ask about Y"]).is_empty()


def test_brief_round_trips_with_phase4_fields():
    """Round-trip a fully-populated brief through JSON and back."""
    original = ClinicalIntakeBrief(
        chief_complaint=ChiefComplaint(verbatim="x", summary="y"),
        hpi=HPI(narrative="A clinically meaningful narrative goes here for a sound test."),
        ros=ROS(),
        patient_identifiers=PatientIdentifiers(name="Sarah", visit_type="urgent"),
        current_medications=[Medication(name="metformin", dose="500mg", frequency="BID")],
        allergies=[Allergy(substance="latex")],
        social_history=SocialHistory(smoking_status="never", alcohol_use="rare"),
        clinician_notes=ClinicianNotes(
            differential_considerations=["consider X"],
            suggested_followups=["ask about Y"],
        ),
        icd10_candidates=[ICD10Candidate(code="R10.9", description="Unspecified abdominal pain")],
        completeness=Completeness(score=72, missing_fields=["pmh"]),
    )
    payload = original.model_dump_json()
    restored = ClinicalIntakeBrief.model_validate_json(payload)
    assert restored.patient_identifiers.name == "Sarah"
    assert restored.patient_identifiers.visit_type == "urgent"
    assert restored.current_medications[0].name == "metformin"
    assert restored.allergies[0].substance == "latex"
    assert restored.social_history.smoking_status == "never"
    assert restored.icd10_candidates[0].code == "R10.9"
    assert restored.completeness.score == 72
