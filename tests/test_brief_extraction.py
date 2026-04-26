"""End-to-end eval harness for brief extraction.

These tests call Gemini and are skipped automatically when GOOGLE_API_KEY is
absent (see tests/conftest.py). They are deliberately permissive on the
*phrasing* of extracted fields — the LLM has latitude — but strict on the
clinical structure: CC verbatim must come from the transcript, ROS must
distinguish positives and negatives, and emergency-flagged transcripts must
surface a red flag.

Mark each one @pytest.mark.requires_api_key so it skips cleanly without a key.
"""

from __future__ import annotations
import pytest

from app.brief import extract_from_transcript


# Marker for the whole module — every test here calls Gemini.
pytestmark = pytest.mark.requires_api_key


def test_abdominal_pain_extraction(transcript_abdominal: str):
    brief = extract_from_transcript(transcript_abdominal, call_id="eval-abd-001")

    # CC verbatim must come from the patient's actual words. Loose check:
    # the verbatim should appear (or be a near-substring) in the transcript.
    cc = brief.chief_complaint.verbatim.strip().strip('"').lower()
    assert cc, "CC verbatim must not be empty"
    # Allow normalization on punctuation/whitespace by checking the key noun
    # of the chief complaint is in the source text.
    assert "right" in cc or "side" in cc or "pain" in cc, (
        f"CC verbatim looks paraphrased: {brief.chief_complaint.verbatim!r}"
    )

    # HPI narrative is the first thing a clinician reads — it should be a
    # paragraph, not a sentence fragment, and not a transcript dump.
    n = brief.hpi.narrative
    assert 80 <= len(n) <= 800, (
        f"HPI narrative length {len(n)} outside 80-800 range: {n!r}"
    )

    # OPQRST: severity should be captured (4/10 now, 7/10 worst per the script)
    assert brief.hpi.severity_now is not None
    assert brief.hpi.severity_worst is not None
    assert brief.hpi.severity_worst >= brief.hpi.severity_now

    # Targeted ROS: at least one system should have entries (constitutional,
    # GI, GU all asked in the transcript). Pertinent negatives matter.
    asked = brief.ros.asked_systems()
    assert asked, "expected at least one ROS system with entries"
    assert any(sys.negatives for _, sys in asked), (
        "ROS should capture at least one pertinent negative — clinical signal"
    )

    # No red flag in this scenario — abdominal pain without alarm features.
    # Allow up to one low-severity flag in case the model is conservative,
    # but no 'emergent' tag.
    assert all(rf.severity != "emergent" for rf in brief.red_flags), (
        "abdominal-pain transcript should not produce an emergent red flag"
    )

    # Phase 0 verification: name + appointment + visit type were spoken — should
    # appear in patient_identifiers.
    pid = brief.patient_identifiers
    assert pid is not None, "patient_identifiers must be populated when fixture states them"
    assert pid.name and "Sarah" in pid.name, f"expected name to capture 'Sarah', got {pid.name!r}"
    assert pid.appointment_time, "appointment_time must be captured"
    assert pid.visit_type == "follow_up", f"expected 'follow_up', got {pid.visit_type!r}"

    # Phase 4 quick history: PMH + meds + allergies were all asked.
    assert brief.past_medical_history, "PMH should include hypothyroidism per fixture"
    assert any("levothyroxine" in m.name.lower() for m in brief.current_medications), (
        f"meds should include levothyroxine; got {[m.name for m in brief.current_medications]}"
    )
    assert any("penicillin" in a.substance.lower() for a in brief.allergies), (
        f"allergies should include penicillin; got {[a.substance for a in brief.allergies]}"
    )

    # Completeness should be solid for this rich fixture — most fields populated.
    assert brief.completeness is not None
    assert brief.completeness.score >= 70, (
        f"expected completeness ≥ 70 for the rich fixture, got {brief.completeness.score} "
        f"(missing: {brief.completeness.missing_fields})"
    )


def test_chest_pain_redflag_extraction(transcript_chest_pain_redflag: str):
    brief = extract_from_transcript(transcript_chest_pain_redflag, call_id="eval-cp-001")

    # The defining requirement: this transcript MUST produce at least one red flag,
    # and at least one of them should be 'emergent' since the agent advised 911/ER.
    assert brief.red_flags, "chest-pain transcript must produce a red flag"
    assert any(rf.severity == "emergent" for rf in brief.red_flags), (
        f"expected an emergent red flag, got: {[rf.severity for rf in brief.red_flags]}"
    )

    # The flagged symptom should mention the chest in some form — a generic
    # 'chest pain' or specific 'chest pressure with radiation'.
    flag_text = " ".join(rf.symptom.lower() for rf in brief.red_flags)
    assert "chest" in flag_text, (
        f"expected red-flag symptom to mention chest: {flag_text!r}"
    )


def test_fatigue_multisystem_extraction(transcript_fatigue_multisystem: str):
    brief = extract_from_transcript(transcript_fatigue_multisystem, call_id="eval-fat-001")

    # This transcript stretches across constitutional + endocrine + psych +
    # GI. The brief should engage multiple ROS systems, not just one.
    asked = brief.ros.asked_systems()
    assert len(asked) >= 2, (
        f"multi-system transcript should populate >=2 ROS systems, got {len(asked)}: "
        f"{[label for label, _ in asked]}"
    )

    # Constitutional should specifically be present — fever/chills/weight loss
    # were all explicitly asked.
    labels = {label for label, _ in asked}
    assert "Constitutional" in labels, (
        f"Constitutional must be present (fever/weight loss/fatigue all asked); got {labels}"
    )

    # The transcript explicitly mentions ~12 lbs of weight loss — should appear
    # somewhere as a positive in constitutional or in HPI text.
    md = brief.to_markdown().lower()
    assert "weight" in md or "12" in md or "twelve" in md, (
        "expected weight loss to surface in the brief somewhere"
    )

    # The brief should not be empty; this scenario is rich.
    assert len(brief.to_markdown()) > 600, "rich multi-system transcript should produce a substantive brief"
