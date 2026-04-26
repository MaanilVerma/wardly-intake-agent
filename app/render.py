"""Chart-style HTML renderer for a saved clinical brief.

Pure function `render_brief(data, call_id) -> str`. Dict in, HTML string out.
The shape of `data` is whatever `ClinicalIntakeBrief.model_dump(mode="json")`
emits — see app/models.py for the canonical schema.
"""

from __future__ import annotations
import html as _html
from datetime import datetime, timezone
from typing import Any


_ROS_LABELS = {
    "constitutional": "Constitutional",
    "heent": "HEENT",
    "cardiac": "Cardiac",
    "respiratory": "Respiratory",
    "gi": "Gastrointestinal",
    "gu": "Genitourinary",
    "msk": "Musculoskeletal",
    "skin": "Skin",
    "neuro": "Neurologic",
    "psych": "Psychiatric",
    "endocrine": "Endocrine",
    "heme_lymph": "Heme / Lymph",
    "allergy_immuno": "Allergy / Immuno",
}

_SEVERITY_LABELS = {
    "concern":  ("Concern",  "sev-concern"),
    "urgent":   ("Urgent",   "sev-urgent"),
    "emergent": ("Emergent", "sev-emergent"),
}

_SEX_SHORT = {"male": "M", "female": "F", "other": "Other"}

_VISIT_TYPE_LABELS = {
    "new_patient": "New patient",
    "follow_up":   "Follow-up",
    "urgent":      "Urgent",
    "telehealth":  "Telehealth",
    "unknown":     "Visit type unconfirmed",
}


def _esc(v: Any) -> str:
    if v is None:
        return ""
    return _html.escape(str(v))


def _fmt_date(iso: str | None) -> tuple[str, str]:
    """Return (date_str, time_str) for a UTC iso timestamp, in local-ish format."""
    if not iso:
        return ("—", "—")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return (_esc(iso), "")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.strftime("%b %d, %Y"), dt.strftime("%H:%M UTC"))


def _fmt_duration(secs: int | None) -> str:
    if secs is None:
        return "—"
    m, s = divmod(int(secs), 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m {s:02d}s"


def _fmt_demographics(d: dict | None, *, compact: bool = False) -> str:
    """Compact form: '47yo F'. Long form: '47 yo, female'.

    Skips pregnancy unless the patient is actually pregnant — 'unknown' or
    'n/a' is just noise on a chart.
    """
    if not d:
        return ""
    bits: list[str] = []
    age = d.get("age")
    sex = d.get("sex")
    if compact:
        if age is not None and sex:
            bits.append(f"{age}yo {_SEX_SHORT.get(sex, sex)}")
        elif age is not None:
            bits.append(f"{age}yo")
        elif sex:
            bits.append(_SEX_SHORT.get(sex, sex))
    else:
        if age is not None:
            bits.append(f"{age} yo")
        if sex:
            bits.append(str(sex).replace("_", " "))
    preg = d.get("pregnancy_status")
    if preg == "pregnant":
        bits.append("pregnant")
    return (" · " if compact else ", ").join(bits)


def _render_red_flags(red_flags: list[dict]) -> str:
    if not red_flags:
        return ""
    items: list[str] = []
    for rf in red_flags:
        sev_key = (rf.get("severity") or "concern").lower()
        sev_label, sev_class = _SEVERITY_LABELS.get(sev_key, ("Concern", "sev-concern"))
        action = rf.get("advised_action") or ""
        action_html = f'<p class="rf-action">Advised: {_esc(action)}</p>' if action else ""
        symptom_html = _esc(rf.get("symptom", ""))
        items.append(
            f'<li class="rf-item">'
            f'  <span class="rf-pill {sev_class}">{sev_label}</span>'
            f'  <div class="rf-body">'
            f'    <p class="rf-symptom">{symptom_html}</p>'
            f'    {action_html}'
            f'  </div>'
            f'</li>'
        )
    return (
        '<section id="redflags" class="cb-section cb-redflag-section">'
        '  <header class="cb-section-head">'
        '    <h2>Red flags</h2>'
        f'    <span class="cb-count">{len(red_flags)}</span>'
        '  </header>'
        '  <ul class="rf-list">' + "".join(items) + '</ul>'
        '</section>'
    )


def _render_chief_complaint(cc: dict) -> str:
    verbatim = _esc(cc.get("verbatim", ""))
    return (
        '<section id="cc" class="cb-section">'
        '  <header class="cb-section-head"><h2>Chief complaint</h2></header>'
        f'  <blockquote class="cc-quote">{verbatim}</blockquote>'
        '  <p class="cc-attrib">Patient&rsquo;s own words</p>'
        '</section>'
    )


def _hpi_rows(hpi: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []

    def add(label: str, value: Any) -> None:
        if value is None or value == "" or value == []:
            return
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        rows.append((label, str(value)))

    add("Onset", hpi.get("onset"))
    add("Location", hpi.get("location"))
    add("Quality", hpi.get("character"))
    add("Radiation", hpi.get("radiation"))

    sev_now = hpi.get("severity_now")
    sev_worst = hpi.get("severity_worst")
    if sev_now is not None or sev_worst is not None:
        bits: list[str] = []
        if sev_now is not None:
            bits.append(f"{sev_now}/10 now")
        if sev_worst is not None:
            bits.append(f"{sev_worst}/10 worst")
        rows.append(("Severity", " · ".join(bits)))

    add("Timing", hpi.get("timing"))
    add("Duration", hpi.get("duration"))
    add("Worse with", hpi.get("aggravating_factors"))
    add("Better with", hpi.get("alleviating_factors"))
    add("Associated", hpi.get("associated_symptoms"))
    add("Treatments tried", hpi.get("treatments_tried"))
    add("Prior episodes", hpi.get("prior_episodes"))
    add("Context", hpi.get("context"))
    return rows


def _severity_band(n: int) -> tuple[str, str]:
    """Return (label, css-class) for a 0-10 pain score."""
    if n >= 7:
        return ("Severe", "sev-band-severe")
    if n >= 4:
        return ("Moderate", "sev-band-moderate")
    if n >= 1:
        return ("Mild", "sev-band-mild")
    return ("None", "sev-band-none")


def _render_severity_meter(now: int | None, worst: int | None) -> str:
    if now is None and worst is None:
        return ""
    n = now if now is not None else (worst or 0)
    pct = max(0, min(100, n * 10))
    band_label, band_class = _severity_band(n)
    worst_html = ""
    if worst is not None and worst != now:
        worst_html = f'<span class="sev-worst">peak {worst}/10</span>'
    return (
        '<div class="sev-meter">'
        '  <div class="sev-meter-head">'
        '    <span class="sev-meter-label">Severity</span>'
        f'   <span class="sev-callout {band_class}"><strong>{n}</strong><span>/10</span> · {band_label}</span>'
        f'   {worst_html}'
        '  </div>'
        '  <div class="sev-bar" aria-hidden="true">'
        f'    <div class="sev-fill {band_class}" style="width:{pct}%"></div>'
        '    <div class="sev-tick" style="left:30%"></div>'
        '    <div class="sev-tick" style="left:70%"></div>'
        '  </div>'
        '  <div class="sev-scale" aria-hidden="true">'
        '    <span>0 None</span><span>3 Mild</span><span>7 Severe</span><span>10</span>'
        '  </div>'
        '</div>'
    )


def _render_hpi(hpi: dict) -> str:
    narrative = _esc(hpi.get("narrative", ""))
    rows = _hpi_rows(hpi)
    rows = [(l, v) for (l, v) in rows if l != "Severity"]
    sev_meter = _render_severity_meter(hpi.get("severity_now"), hpi.get("severity_worst"))
    rows_html = "".join(
        f'<div class="dl-row"><dt>{_esc(label)}</dt><dd>{_esc(value)}</dd></div>'
        for label, value in rows
    )
    return (
        '<section id="hpi" class="cb-section">'
        '  <header class="cb-section-head"><h2>History of present illness</h2></header>'
        f'  <p class="cb-narrative">{narrative}</p>'
        f'  {sev_meter}'
        f'  <dl class="dl-list">{rows_html}</dl>'
        '</section>'
    )


def _render_ros(ros: dict) -> str:
    rows: list[str] = []
    for key, label in _ROS_LABELS.items():
        sys = ros.get(key)
        if not sys:
            continue
        positives = sys.get("positives") or []
        negatives = sys.get("negatives") or []
        if not positives and not negatives:
            continue
        pos_html = (
            "".join(f'<span class="ros-tag pos">{_esc(p)}</span>' for p in positives)
            if positives else '<span class="ros-empty">—</span>'
        )
        neg_html = (
            "".join(f'<span class="ros-tag neg">{_esc(n)}</span>' for n in negatives)
            if negatives else '<span class="ros-empty">—</span>'
        )
        rows.append(
            f'<tr>'
            f'  <th scope="row">{_esc(label)}</th>'
            f'  <td>{pos_html}</td>'
            f'  <td>{neg_html}</td>'
            f'</tr>'
        )
    if not rows:
        return ""
    return (
        '<section id="ros" class="cb-section">'
        '  <header class="cb-section-head"><h2>Review of systems</h2></header>'
        '  <div class="cb-table-wrap">'
        '    <table class="ros-table">'
        '      <thead><tr><th scope="col">System</th><th scope="col">Positive</th><th scope="col">Negative</th></tr></thead>'
        f'      <tbody>{"".join(rows)}</tbody>'
        '    </table>'
        '  </div>'
        '</section>'
    )


def _render_intake_notes(notes: str | None) -> str:
    if not notes:
        return ""
    return (
        '<section id="notes" class="cb-section">'
        '  <header class="cb-section-head"><h2>Intake notes</h2></header>'
        f'  <p class="cb-narrative">{_esc(notes)}</p>'
        '</section>'
    )


def _fmt_appointment(verbatim: str | None, iso: str | None) -> str:
    """Prefer the resolved ISO datetime ('Apr 27, 2026 · 2:00 PM').

    If we have both, render ISO with the verbatim as a tooltip so the
    patient's phrasing isn't lost. Fall back to verbatim if ISO is missing.
    Uses portable strftime (no `%-d` / `%-I`) so it works on Windows too.
    """
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            day = int(dt.strftime("%d"))
            hour = int(dt.strftime("%I"))
            label = f'{dt.strftime("%b")} {day}, {dt.year} · {hour}:{dt.strftime("%M %p")}'
        except ValueError:
            label = iso
        if verbatim and verbatim.strip().lower() != label.lower():
            return f'<span class="pb-appt" title="Patient said: {_esc(verbatim)}">📅 {_esc(label)}</span>'
        return f'<span class="pb-appt">📅 {_esc(label)}</span>'
    if verbatim:
        return f'<span class="pb-appt">📅 {_esc(verbatim)}</span>'
    return ""


def _render_patient_band(pid: dict | None, demographics: dict | None) -> str:
    """Top-of-brief one-liner — what a clinician scans first.

    Layout: NAME big, then age/sex · pronouns · appt · visit-type chip.
    Pregnancy 'unknown' is filtered out by _fmt_demographics.
    """
    if not pid and not demographics:
        return ""
    name = (pid or {}).get("name") or "(name not captured)"
    pronouns = (pid or {}).get("preferred_pronouns")
    visit = (pid or {}).get("visit_type")
    visit_label = _VISIT_TYPE_LABELS.get(visit or "", "")
    reason = (pid or {}).get("reason_for_visit_today")

    bits: list[str] = []
    age_sex = _fmt_demographics(demographics, compact=True).strip()
    if age_sex:
        bits.append(_esc(age_sex))
    if pronouns:
        bits.append(_esc(pronouns))
    appt_html = _fmt_appointment(
        (pid or {}).get("appointment_time"),
        (pid or {}).get("appointment_time_iso"),
    )
    if appt_html:
        bits.append(appt_html)
    if visit_label:
        bits.append(f'<span class="pb-visit">{_esc(visit_label)}</span>')

    chips_html = " · ".join(bits) if bits else "<em>Demographics not captured</em>"
    reason_html = (
        f'<p class="pb-reason"><span class="pb-reason-label">Today:</span> {_esc(reason)}</p>'
        if reason else ""
    )
    return (
        '<aside class="cb-patient-band" aria-label="Patient identification">'
        f'  <h2 class="pb-name">{_esc(name)}</h2>'
        f'  <p class="pb-meta">{chips_html}</p>'
        f'  {reason_html}'
        '</aside>'
    )


def _render_pmh_meds_allergies(brief: dict) -> str:
    pmh = brief.get("past_medical_history") or []
    meds = brief.get("current_medications") or []
    allergies = brief.get("allergies") or []
    if not (pmh or meds or allergies):
        return ""

    pmh_html = ""
    if pmh:
        items = "".join(f'<li>{_esc(c)}</li>' for c in pmh)
        pmh_html = (
            '<div class="pmh-block">'
            '  <h3 class="pmh-title">Past medical history</h3>'
            f'  <ul class="pmh-list">{items}</ul>'
            '</div>'
        )

    meds_html = ""
    if meds:
        rows: list[str] = []
        for m in meds:
            name = _esc(m.get("name", ""))
            dose = _esc(m.get("dose") or "—")
            freq = _esc(m.get("frequency") or "—")
            rows.append(
                f'<tr><th scope="row">{name}</th><td>{dose}</td><td>{freq}</td></tr>'
            )
        meds_html = (
            '<div class="pmh-block">'
            '  <h3 class="pmh-title">Current medications</h3>'
            '  <div class="cb-table-wrap">'
            '    <table class="meds-table">'
            '      <thead><tr><th>Medication</th><th>Dose</th><th>Frequency</th></tr></thead>'
            f'      <tbody>{"".join(rows)}</tbody>'
            '    </table>'
            '  </div>'
            '</div>'
        )

    allergies_html = ""
    if allergies:
        rows = []
        for a in allergies:
            sub = _esc(a.get("substance", ""))
            rxn = _esc(a.get("reaction") or "—")
            rows.append(f'<li><strong>{sub}</strong> <span class="all-rxn">→ {rxn}</span></li>')
        allergies_html = (
            '<div class="pmh-block">'
            '  <h3 class="pmh-title">Allergies</h3>'
            f'  <ul class="all-list">{"".join(rows)}</ul>'
            '</div>'
        )

    return (
        '<section id="pmh" class="cb-section">'
        '  <header class="cb-section-head"><h2>History &amp; medications</h2></header>'
        f'  {pmh_html}'
        f'  {meds_html}'
        f'  {allergies_html}'
        '</section>'
    )


def _render_social(social: dict | None) -> str:
    """Only emit the social-history section when something concrete was disclosed.

    `unknown` and empty strings are noise — a section saying 'unknown · unknown ·
    unknown · unknown' makes the brief look like the intake didn't ask, which is
    worse than not showing it at all.
    """
    if not social:
        return ""
    NULLISH = {None, "", "unknown", "n/a"}

    def _val(key: str) -> str | None:
        v = social.get(key)
        if v is None:
            return None
        s = str(v).strip()
        return s if s.lower() not in NULLISH else None

    bits: list[str] = []
    smk = _val("smoking_status")
    if smk:
        bits.append(f'Smoking: <strong>{_esc(smk.replace("_", " "))}</strong>')
    alc = _val("alcohol_use")
    if alc:
        bits.append(f'Alcohol: {_esc(alc)}')
    drugs = _val("recreational_drugs")
    if drugs:
        bits.append(f'Drugs: {_esc(drugs)}')
    occ = _val("occupation")
    if occ:
        bits.append(f'Occupation: {_esc(occ)}')
    if not bits:
        return ""
    return (
        '<section id="social" class="cb-section">'
        '  <header class="cb-section-head"><h2>Social history</h2></header>'
        f'  <p class="cb-social">{" · ".join(bits)}</p>'
        '</section>'
    )


def _render_ai_panel(notes: dict | None, codes: list[dict] | None) -> str:
    notes = notes or {}
    diff = notes.get("differential_considerations") or []
    follow = notes.get("suggested_followups") or []
    codes = codes or []
    if not (diff or follow or codes):
        return ""

    diff_html = ""
    if diff:
        items = "".join(f'<li>{_esc(d)}</li>' for d in diff[:4])
        diff_html = (
            '<div class="ai-sub">'
            '  <h3 class="ai-sub-title">Differential considerations</h3>'
            f'  <ul class="ai-list">{items}</ul>'
            '</div>'
        )

    follow_html = ""
    if follow:
        items = "".join(f'<li>{_esc(f)}</li>' for f in follow[:2])
        follow_html = (
            '<div class="ai-sub">'
            '  <h3 class="ai-sub-title">Suggested follow-ups</h3>'
            f'  <ul class="ai-list">{items}</ul>'
            '</div>'
        )

    codes_html = ""
    if codes:
        chips = "".join(
            f'<span class="icd-chip"><span class="icd-code">{_esc(c.get("code",""))}</span>'
            f'<span class="icd-desc">{_esc(c.get("description",""))}</span></span>'
            for c in codes[:3]
        )
        codes_html = (
            '<div class="ai-sub">'
            '  <h3 class="ai-sub-title">Candidate ICD-10 codes</h3>'
            f'  <div class="icd-row">{chips}</div>'
            '</div>'
        )

    return (
        '<section id="ai" class="cb-section cb-ai-panel">'
        '  <header class="cb-section-head ai-head">'
        '    <h2><span class="ai-spark" aria-hidden="true">✨</span> AI suggestions</h2>'
        '    <span class="ai-disclaimer">Not a diagnosis · Generated from this transcript · For clinician review</span>'
        '  </header>'
        f'  {diff_html}'
        f'  {follow_html}'
        f'  {codes_html}'
        '</section>'
    )


def _render_completeness(c: dict | None) -> str:
    if not c:
        return ""
    score = int(c.get("score") or 0)
    if score >= 80:
        band = "good"
    elif score >= 60:
        band = "ok"
    else:
        band = "low"
    missing = c.get("missing_fields") or []
    title = " · ".join(_esc(m) for m in missing) if missing else "All canonical fields captured"
    return (
        f'<span class="cb-completeness band-{band}" title="Missing: {title}">'
        f'  <span class="cmp-label">Completeness</span>'
        f'  <span class="cmp-score">{score}%</span>'
        '</span>'
    )


def _render_sidebar(items: list[tuple[str, str, bool]]) -> str:
    """items: list of (anchor, label, is_alert)."""
    links = "".join(
        f'<a href="#{anchor}" class="cb-nav-link{" is-alert" if alert else ""}">'
        f'<span class="cb-nav-dot" aria-hidden="true"></span>{_esc(label)}</a>'
        for anchor, label, alert in items
    )
    return f'<nav class="cb-nav" aria-label="Brief sections">{links}</nav>'


def _render_structured_brief(data: dict, call_id: str) -> str:
    meta = data.get("call_metadata") or {}
    started_at = meta.get("started_at") if meta else None
    date_str, time_str = _fmt_date(started_at)
    duration = _fmt_duration(meta.get("duration_sec") if meta else None)

    red_flags = data.get("red_flags") or []
    has_red_flags = len(red_flags) > 0
    cc = data.get("chief_complaint") or {}
    cc_summary = cc.get("summary") or "—"
    hpi = data.get("hpi") or {}

    nav: list[tuple[str, str, bool]] = []
    if has_red_flags:
        nav.append(("redflags", "Red flags", True))
    nav.append(("cc",  "Chief complaint", False))
    nav.append(("hpi", "History of present illness", False))
    if any((data.get("ros") or {}).get(k) for k in _ROS_LABELS):
        nav.append(("ros", "Review of systems", False))
    if data.get("past_medical_history") or data.get("current_medications") or data.get("allergies"):
        nav.append(("pmh", "History & medications", False))
    soc = data.get("social_history") or {}
    NULLISH_SOC = {None, "", "unknown", "n/a"}
    if any(
        (str(soc.get(k) or "").strip().lower() not in NULLISH_SOC)
        for k in ("smoking_status", "alcohol_use", "recreational_drugs", "occupation")
    ):
        nav.append(("social", "Social history", False))
    notes_obj = data.get("clinician_notes") or {}
    if notes_obj.get("differential_considerations") or notes_obj.get("suggested_followups") or data.get("icd10_candidates"):
        nav.append(("ai", "AI suggestions", False))
    if data.get("intake_notes"):
        nav.append(("notes", "Intake notes", False))

    alert_strip = ""
    if has_red_flags:
        worst = max(red_flags, key=lambda r: ["concern", "urgent", "emergent"].index((r.get("severity") or "concern").lower()))
        sev_key = (worst.get("severity") or "concern").lower()
        sev_label, sev_class = _SEVERITY_LABELS.get(sev_key, ("Concern", "sev-concern"))
        s = "s" if len(red_flags) != 1 else ""
        alert_strip = (
            f'<div class="cb-alert {sev_class}">'
            f'  <span class="cb-alert-pill">{sev_label}</span>'
            f'  <span class="cb-alert-text"><strong>{len(red_flags)} red-flag finding{s}</strong> · review immediately</span>'
            f'  <a href="#redflags" class="cb-alert-link">Jump to flags →</a>'
            f'</div>'
        )

    key_chips: list[str] = []
    sev_now = hpi.get("severity_now")
    if sev_now is not None:
        band_label, band_class = _severity_band(sev_now)
        key_chips.append(f'<span class="kc"><span class="kc-label">Severity</span><span class="kc-value {band_class}">{sev_now}/10 · {band_label}</span></span>')
    if hpi.get("onset"):
        key_chips.append(f'<span class="kc"><span class="kc-label">Onset</span><span class="kc-value">{_esc(hpi.get("onset"))}</span></span>')
    if hpi.get("location"):
        key_chips.append(f'<span class="kc"><span class="kc-label">Location</span><span class="kc-value">{_esc(hpi.get("location"))}</span></span>')
    if has_red_flags:
        key_chips.append(f'<span class="kc kc-flag"><span class="kc-label">Flags</span><span class="kc-value">{len(red_flags)}</span></span>')
    key_strip = f'<div class="key-strip">{"".join(key_chips)}</div>' if key_chips else ""

    # Encounter chrome — patient identifier band lives separately, below.
    meta_row_items: list[str] = [
        f'<div class="meta-row"><dt>Encounter</dt><dd>{_esc(date_str)} <span class="meta-sub">· {_esc(time_str)}</span></dd></div>'
    ]
    # Skip duration on recoveries (started_at == ended_at, so duration is 0s).
    if (meta or {}).get("duration_sec"):
        meta_row_items.append(
            f'<div class="meta-row"><dt>Call duration</dt><dd>{_esc(duration)}</dd></div>'
        )
    meta_row_items.append(
        f'<div class="meta-row"><dt>Call ID</dt><dd class="mono">{_esc(call_id)}</dd></div>'
    )
    meta_rows = '<dl class="meta-rows">' + "".join(meta_row_items) + '</dl>'

    body = (
        '<div class="cb-layout">'
        + _render_sidebar(nav) +
        '<article class="cb">'
        '  <header class="cb-header">'
        f'   <div class="cb-eyebrow">Pre-Visit Brief · #{_esc(call_id[:8])}</div>'
        f'   <h1 class="cb-title">{_esc(cc_summary)}</h1>'
        f'   {key_strip}'
        f'   {meta_rows}'
        '  </header>'
        f' {_render_patient_band(data.get("patient_identifiers"), data.get("patient_demographics"))}'
        f' {alert_strip}'
        f' {_render_red_flags(red_flags)}'
        f' {_render_chief_complaint(cc)}'
        f' {_render_hpi(hpi)}'
        f' {_render_ros(data.get("ros") or {})}'
        f' {_render_pmh_meds_allergies(data)}'
        f' {_render_social(data.get("social_history"))}'
        f' {_render_ai_panel(data.get("clinician_notes"), data.get("icd10_candidates"))}'
        f' {_render_intake_notes(data.get("intake_notes"))}'
        '  <footer class="cb-foot">'
        f'   {_render_completeness(data.get("completeness"))}'
        '    <p>Generated by Wardly Pre-Visit Intake. Not a diagnosis. Information collected from the patient, structured for clinician review.</p>'
        '  </footer>'
        '</article>'
        '</div>'
    )
    return body


def render_brief(data: dict, call_id: str) -> str:
    """Render the saved brief JSON as a full chart-style HTML document."""
    body = _render_structured_brief(data, call_id)
    return _BRIEF_HTML_SHELL.format(call_id=_html.escape(call_id), body=body)


def render_markdown_fallback(html_body: str, call_id: str) -> str:
    """Wrap pre-rendered Markdown HTML in the same chrome as the structured view.

    Used when only the .md brief is on disk (the .json was lost) — the
    clinician still gets the Wardly topbar + page chrome around it.
    """
    body = f'<article class="cb">{html_body}</article>'
    return _BRIEF_HTML_SHELL.format(call_id=_html.escape(call_id), body=body)


_BRIEF_HTML_SHELL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b1220" />
<title>Brief {call_id} — Wardly</title>
<link rel="preconnect" href="https://rsms.me/" />
<link rel="stylesheet" href="https://rsms.me/inter/inter.css" />
<link rel="stylesheet" href="/static/style.css" />
</head>
<body class="brief-view">
<header class="topbar" role="banner">
  <div class="topbar-left">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="18" height="18"><path d="M6 8h4l3 12 3-9 3 9 3-12h4" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <span class="brand-name">Wardly</span>
    </div>
    <span class="brand-divider" aria-hidden="true"></span>
    <span class="brand-product">Clinical Brief</span>
  </div>
  <div class="topbar-right">
    <a class="btn" href="/">
      <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M10 12 6 8l4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Back to intake
    </a>
    <button class="btn btn-primary" onclick="window.print()">
      <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M4 6V2h8v4M4 12H2v-4h12v4h-2M4 10h8v4H4z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
      Print / PDF
    </button>
  </div>
</header>
<main class="brief-page">
{body}
</main>
<script>
  // Scroll-spy: highlight the sidebar link for the section currently in view.
  (function () {{
    var nav = document.querySelector('.cb-nav');
    if (!nav) return;
    var links = Array.prototype.slice.call(nav.querySelectorAll('.cb-nav-link'));
    var sections = links.map(function (l) {{
      return document.getElementById(l.getAttribute('href').slice(1));
    }}).filter(Boolean);
    if (!('IntersectionObserver' in window)) return;
    var obs = new IntersectionObserver(function (entries) {{
      entries.forEach(function (e) {{
        if (!e.isIntersecting) return;
        var id = e.target.id;
        links.forEach(function (l) {{
          l.classList.toggle('is-current', l.getAttribute('href') === '#' + id);
        }});
      }});
    }}, {{ rootMargin: '-30% 0px -60% 0px', threshold: 0 }});
    sections.forEach(function (s) {{ obs.observe(s); }});
  }}());
</script>
</body>
</html>
"""


def render_brief_index(items: list[dict]) -> str:
    """Render the list of all saved briefs as a single HTML page.

    `items` is whatever `app.storage.list_briefs()` returns — already sorted
    newest first. Each row links to `/briefs/{call_id}` (the chart view).

    Empty state: friendly message with a link back to the intake page.
    """
    if not items:
        body = (
            '<section class="briefs-empty">'
            '  <div class="briefs-empty-icon" aria-hidden="true">'
            '    <svg viewBox="0 0 24 24" width="32" height="32">'
            '      <path d="M5 4h11l3 3v13H5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'
            '      <path d="M16 4v3h3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'
            '    </svg>'
            '  </div>'
            '  <h2>No briefs yet</h2>'
            '  <p>Once you complete an intake, the brief will land here.</p>'
            '  <a class="btn btn-primary" href="/">Start an intake</a>'
            '</section>'
        )
    else:
        rows = "".join(_render_brief_row(item) for item in items)
        body = (
            '<header class="briefs-head">'
            '  <div>'
            f'    <h1>All briefs <span class="briefs-count">{len(items)}</span></h1>'
            '    <p class="briefs-sub">Sorted newest first. Click any row to open the chart view.</p>'
            '  </div>'
            '  <a class="btn btn-primary" href="/">'
            '    <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M3 8h10m-4-4 4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            '    Start a new intake'
            '  </a>'
            '</header>'
            f'<div class="briefs-list" role="list">{rows}</div>'
        )

    return _INDEX_HTML_SHELL.format(body=body, count=len(items))


def _render_brief_row(item: dict) -> str:
    call_id = _esc(item.get("call_id") or "")
    name = item.get("patient_name") or "Unnamed patient"
    summary = item.get("summary") or item.get("verbatim") or "(no chief complaint captured)"
    summary = summary.strip().strip('"').strip("'")
    if len(summary) > 140:
        summary = summary[:137].rstrip() + "…"

    when_abs, when_rel = _fmt_when(item.get("started_at"), item.get("mtime"))
    visit_type = _VISIT_TYPE_LABELS.get(item.get("visit_type") or "", None)
    duration = _fmt_duration(item.get("duration_sec")) if item.get("duration_sec") else None

    severity = item.get("severity_worst")
    severity_chip = ""
    if isinstance(severity, int):
        band = "good" if severity <= 3 else "ok" if severity <= 6 else "low"
        severity_chip = (
            f'<span class="bx-chip bx-chip-{band}" title="Worst severity reported">'
            f'  <strong>{severity}/10</strong> worst'
            '</span>'
        )

    redflag_chip = ""
    rf_count = item.get("red_flag_count") or 0
    if rf_count:
        sev_key = (item.get("red_flag_severity") or "concern").lower()
        sev_label, sev_class = _SEVERITY_LABELS.get(sev_key, ("Concern", "sev-concern"))
        redflag_chip = (
            f'<span class="bx-chip bx-chip-redflag {sev_class}" title="Red flag: {sev_label}">'
            '  <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"><path d="M3 14V2l9 4-9 4" fill="currentColor"/></svg>'
            f'  {rf_count} red flag{"s" if rf_count != 1 else ""} · {sev_label}'
            '</span>'
        )

    completeness = item.get("completeness")
    completeness_chip = ""
    if isinstance(completeness, int):
        band = "good" if completeness >= 80 else "ok" if completeness >= 60 else "low"
        completeness_chip = (
            f'<span class="bx-chip bx-chip-{band}" title="Completeness score">'
            f'  {completeness}% complete'
            '</span>'
        )

    visit_pill = (
        f'<span class="bx-pill">{_esc(visit_type)}</span>' if visit_type else ""
    )
    duration_pill = (
        f'<span class="bx-pill bx-pill-muted" title="Call duration">⏱ {_esc(duration)}</span>' if duration else ""
    )

    return (
        f'<a class="bx-row" href="/briefs/{call_id}" role="listitem">'
        '  <div class="bx-row-main">'
        '    <div class="bx-when">'
        f'      <span class="bx-when-rel">{_esc(when_rel)}</span>'
        f'      <span class="bx-when-abs">{_esc(when_abs)}</span>'
        '    </div>'
        '    <div class="bx-body">'
        '      <div class="bx-headline">'
        f'        <span class="bx-name">{_esc(name)}</span>'
        f'        {visit_pill}'
        '      </div>'
        f'      <p class="bx-summary">{_esc(summary)}</p>'
        '      <div class="bx-meta">'
        f'        {severity_chip}'
        f'        {redflag_chip}'
        f'        {completeness_chip}'
        f'        {duration_pill}'
        f'        <code class="bx-id">{call_id[:8]}…</code>'
        '      </div>'
        '    </div>'
        '    <span class="bx-go" aria-hidden="true">'
        '      <svg viewBox="0 0 16 16" width="14" height="14"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '    </span>'
        '  </div>'
        '</a>'
    )


def _fmt_when(started_at: str | None, mtime: float | None) -> tuple[str, str]:
    """Return (absolute label, relative label) — falls back to mtime."""
    dt: datetime | None = None
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None and mtime is not None:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    if dt is None:
        return ("—", "—")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    abs_label = dt.strftime("%b %d, %Y · %H:%M UTC")
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        rel = "just now"
    elif secs < 3600:
        m = secs // 60
        rel = f"{m} min ago"
    elif secs < 86_400:
        h = secs // 3600
        rel = f"{h} hr ago"
    elif secs < 7 * 86_400:
        d = secs // 86_400
        rel = "yesterday" if d == 1 else f"{d} days ago"
    else:
        rel = dt.strftime("%b %d")
    return (abs_label, rel)


_INDEX_HTML_SHELL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b1220" />
<title>All briefs ({count}) — Wardly</title>
<link rel="preconnect" href="https://rsms.me/" />
<link rel="stylesheet" href="https://rsms.me/inter/inter.css" />
<link rel="stylesheet" href="/static/style.css" />
<style>
  /* Scoped to the briefs index page. Built on the existing style.css tokens
     (--brand-*, --ink-*, --shadow-*, --radius-*) so it visually matches
     the rest of the app without bloating the main stylesheet. */
  .briefs-page {{
    max-width: 980px;
    margin: 0 auto;
    padding: clamp(20px, 3vw, 36px) clamp(16px, 3vw, 32px) 56px;
  }}
  .briefs-head {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 22px;
  }}
  .briefs-head h1 {{
    margin: 0 0 4px 0;
    font-size: clamp(22px, 2vw + 14px, 30px);
    font-weight: 650;
    letter-spacing: -0.018em;
    color: var(--ink-900);
  }}
  .briefs-count {{
    display: inline-block;
    margin-left: 8px;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--brand-50);
    color: var(--brand-700);
    font-size: 13px;
    font-weight: 600;
    vertical-align: 4px;
  }}
  .briefs-sub {{
    margin: 0;
    color: var(--ink-500);
    font-size: 14px;
  }}

  .briefs-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .bx-row {{
    display: block;
    background: var(--paper);
    border: 1px solid var(--ink-100);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-xs);
    text-decoration: none;
    color: inherit;
    transition: border-color var(--dur-2) var(--ease-out),
                box-shadow var(--dur-2) var(--ease-out),
                transform var(--dur-1) var(--ease-out);
  }}
  .bx-row:hover {{
    border-color: var(--brand-100);
    box-shadow: var(--shadow-md);
  }}
  .bx-row:active {{ transform: translateY(1px); }}
  .bx-row-main {{
    display: grid;
    grid-template-columns: 130px 1fr auto;
    gap: 18px;
    align-items: center;
    padding: 14px 18px;
  }}
  @media (max-width: 640px) {{
    .bx-row-main {{ grid-template-columns: 1fr auto; }}
    .bx-when {{ grid-row: 2; grid-column: 1 / -1; padding-top: 6px; border-top: 1px dashed var(--ink-100); }}
  }}
  .bx-when {{
    display: flex;
    flex-direction: column;
    gap: 2px;
    color: var(--ink-600);
    font-size: 13px;
  }}
  .bx-when-rel {{
    font-weight: 600;
    color: var(--ink-700);
  }}
  .bx-when-abs {{
    font-size: 11.5px;
    color: var(--ink-500);
    font-feature-settings: 'tnum';
  }}
  .bx-body {{ min-width: 0; }}
  .bx-headline {{
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }}
  .bx-name {{
    font-weight: 600;
    color: var(--ink-900);
    font-size: 15px;
    letter-spacing: -0.005em;
  }}
  .bx-summary {{
    margin: 0 0 8px 0;
    color: var(--ink-700);
    font-size: 14px;
    line-height: 1.45;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }}
  .bx-meta {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 8px;
    font-size: 12px;
  }}
  .bx-id {{
    font-family: 'SF Mono', ui-monospace, Menlo, monospace;
    font-size: 11px;
    color: var(--ink-400);
  }}
  .bx-go {{
    display: inline-grid;
    place-items: center;
    color: var(--ink-300);
    transition: color var(--dur-1) var(--ease-out), transform var(--dur-1) var(--ease-out);
  }}
  .bx-row:hover .bx-go {{
    color: var(--brand-600);
    transform: translateX(2px);
  }}

  .bx-pill {{
    display: inline-flex;
    align-items: center;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--ink-100);
    color: var(--ink-700);
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.01em;
    text-transform: uppercase;
  }}
  .bx-pill-muted {{
    background: transparent;
    color: var(--ink-500);
    text-transform: none;
    letter-spacing: 0;
    font-weight: 500;
  }}

  .bx-chip {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 9px 2px 8px;
    border-radius: 999px;
    background: var(--ink-50);
    color: var(--ink-700);
    border: 1px solid var(--ink-100);
    font-weight: 500;
  }}
  .bx-chip-good {{ background: var(--good-50); color: var(--good-600); border-color: #bbf7d0; }}
  .bx-chip-ok   {{ background: var(--warn-50); color: var(--warn-700); border-color: #fde68a; }}
  .bx-chip-low  {{ background: var(--danger-50); color: var(--danger-700); border-color: #fecaca; }}
  .bx-chip-redflag.sev-emergent {{ background: var(--danger-50); color: var(--danger-700); border-color: #fecaca; font-weight: 600; }}
  .bx-chip-redflag.sev-urgent   {{ background: var(--warn-50); color: var(--warn-700); border-color: #fde68a; font-weight: 600; }}
  .bx-chip-redflag.sev-concern  {{ background: var(--brand-50); color: var(--brand-700); border-color: var(--brand-100); font-weight: 600; }}

  .briefs-empty {{
    text-align: center;
    padding: 80px 32px;
    background: var(--paper);
    border: 1px dashed var(--ink-200);
    border-radius: var(--radius-xl);
    color: var(--ink-500);
  }}
  .briefs-empty-icon {{
    display: inline-grid;
    place-items: center;
    width: 56px; height: 56px;
    border-radius: 16px;
    background: var(--brand-50);
    color: var(--brand-600);
    margin-bottom: 14px;
  }}
  .briefs-empty h2 {{
    margin: 0 0 6px 0;
    color: var(--ink-900);
    font-size: 18px;
  }}
  .briefs-empty p {{ margin: 0 0 16px 0; }}
</style>
</head>
<body class="brief-view">
<header class="topbar" role="banner">
  <div class="topbar-left">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="18" height="18"><path d="M6 8h4l3 12 3-9 3 9 3-12h4" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <span class="brand-name">Wardly</span>
    </div>
    <span class="brand-divider" aria-hidden="true"></span>
    <span class="brand-product">All Briefs</span>
  </div>
  <div class="topbar-right">
    <a class="btn" href="/">
      <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M10 12 6 8l4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      New intake
    </a>
  </div>
</header>
<main class="briefs-page">
{body}
</main>
</body>
</html>
"""


__all__ = ["render_brief", "render_markdown_fallback", "render_brief_index"]
