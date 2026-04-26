"""Naturalness lint for the live conversation system prompt.

Two static tests (no API call, run on every `pytest`) protect the prompt's
structure from accidental regressions: the persona, length budget, forbidden-
phrases table, capture contract, red-flag block, and the two few-shot
exchanges must all be present, and the file must stay under a modest size
budget so the prompt doesn't bloat back to clipboard length over time.

One live eval test (gated on GOOGLE_API_KEY) drives a six-turn conversation
through `chat_turn()` against the real model and checks the agent's actual
replies for the scriptiness patterns the founder flagged after live testing
("smooth, but stretched / following a script"). The forbidden-phrase regexes
encode that feedback as enforceable contracts, not vibes.
"""

from __future__ import annotations
import re

import pytest

from app.chat import GREETING
from app.llm import ChatMessage, LLMQuotaExhausted, chat_turn
from app.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_PATH

# ---------- static tests --------------------------------------------------

# Required section headers in the current system prompt. Anchored on the
# heading text rather than HTML-comment markers — the prompt structure has
# evolved with each rewrite and section markers don't survive a real-Claude
# pass. These header names are stable as long as the prompt's clinical
# spine is intact (CC/HPI/ROS/Background/Wrap-up/Red flags/Anti-patterns).
REQUIRED_SECTION_HEADERS = [
    "## Voice channel rules",     # the conversational quality bar
    "## LISTEN FIRST",            # the don't-repeat-the-patient rule
    "## What you need by the end of the call",  # capture contract
    "## Pain/symptom drilldown",  # OPQRST without the framework name
    "## Targeted ROS",            # ROS section
    "## Red flags",               # red-flag interrupt
    "## Background",              # PMH / meds / allergies
    "## Wrap-up",                 # the readback
    "## Anti-patterns",           # explicit don'ts
]

# Behavioral rules the prompt MUST keep — these are the lessons paid for in
# real failed calls. If a rewrite drops one of these, we've regressed.
REQUIRED_BEHAVIORAL_RULES = [
    "SPEAK the redirect script FIRST",   # red-flag ordering (Sindant call w/ no script)
    "Banned closings when a red flag is active",  # bleeding-leg call
]

# 30,000 bytes is a regression tripwire, not an aggressive cap. The goal
# of the prompt is clinical-conversation *quality*, not byte count.
# This test catches accidental bloat (e.g. a table getting copy-pasted
# twice) without forcing the prompt to compromise on output quality.
#
# History:
#  -  9,508 bytes  (original Phase-A prompt)
#  -  9,750 bytes  (Maria rewrite + bracket fix)
#  - 19,000 bytes  (real-Claude rewrite with full conversational rules,
#                   batched-question guidance, listen-first section)
#  - 20,161 bytes  (added strict-order red-flag rules + banned closings
#                   list after the bleeding-leg call where Maria fired
#                   `flag_red_flag` but never spoke the 911 advisory)
#
# Raise it consciously when a deliberate addition pushes past it.
MAX_PROMPT_BYTES = 30000


def test_prompt_has_required_sections():
    """Every clinical section we depend on must be present in the prompt.

    Anchored on Markdown heading text. As long as these headers exist, the
    prompt's structure covers the clinical spine. Editors are free to
    reword anything else, add new sections, or restructure the body — but
    these headers staying alive is what guarantees the post-hoc extractor
    has data to extract.
    """
    text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    missing = [h for h in REQUIRED_SECTION_HEADERS if h not in text]
    assert not missing, (
        f"system_prompt.md is missing required section headers: {missing}. "
        f"Each is the prefix of a Markdown heading the rest of the prompt depends on."
    )


def test_prompt_keeps_behavioral_rules():
    """Behavioral rules paid for in real failed calls must stay in the prompt.

    Each rule's presence here corresponds to a specific call where the agent
    failed in a particular way. Removing one is a real regression — surface
    it loudly so the next edit can decide whether to put it back or override
    explicitly.
    """
    text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    missing = [r for r in REQUIRED_BEHAVIORAL_RULES if r not in text]
    assert not missing, (
        f"system_prompt.md is missing required behavioral rules: {missing}. "
        f"Each rule was added because of a specific failed call — see the "
        f"comment next to it in REQUIRED_BEHAVIORAL_RULES."
    )


def test_prompt_size_regression_tripwire():
    """Catch *accidental* bloat — not force compression at the expense of quality.

    The right size is whatever produces the best on-call experience. This
    test only catches the failure mode where the prompt accidentally grows
    (a table copy-pasted twice, an example tripled). Raise the ceiling
    consciously if a deliberate addition needs more room.
    """
    size = SYSTEM_PROMPT_PATH.stat().st_size
    assert size <= MAX_PROMPT_BYTES, (
        f"system_prompt.md is {size} bytes; tripwire at {MAX_PROMPT_BYTES}. "
        f"If this growth was intentional, raise MAX_PROMPT_BYTES with a comment."
    )


def test_prompt_names_the_persona():
    """The prompt should establish a named persona (currently 'Maria') so the
    live conversation has a consistent identity across web and Vapi transports.
    The Vapi assistant's `name` field is also 'Maria' — keep them in sync."""
    assert "Maria" in SYSTEM_PROMPT, "Persona name 'Maria' must appear in the prompt"


# ---------- live naturalness test (API-gated) ------------------------------

# Each agent reply is checked against these patterns. They encode the
# "feels like a script" failure mode the founder reported. Case-insensitive,
# anchored to turn start where it matters (acknowledgments) and free-floating
# where it matters (meta-narration).
_FORBIDDEN_TURN_START = [
    re.compile(r"^\s*got\s*it[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*great[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*perfect[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*wonderful[\s,.\-—!]", re.IGNORECASE),
    # "Thanks, [name]" / "Thank you, [name]" / standalone "Thanks." at turn start.
    # Broadened to any name (founder's real call had "Thanks, Sindant" / "Thanks, Sindhin"
    # — the original pattern only caught Sarah).
    re.compile(r"^\s*thanks?[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*thank\s+you[\s,.\-—!]", re.IGNORECASE),
    # Other stock acknowledgment prefixes the model defaults to under randomness
    re.compile(r"^\s*okay[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*ok[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*alright[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*all right[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*sure[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*right[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*understood[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*got\s+you[\s,.\-—!]", re.IGNORECASE),
    re.compile(r"^\s*i\s+see[\s,.\-—!]", re.IGNORECASE),
    # Echo-paraphrase pattern from the real call: "Okay. So you're..." / "So [paraphrase]".
    # Even after a non-acknowledgment opener, "So" used as a paraphrase-launcher reads
    # like the agent is processing out loud rather than listening.
    re.compile(r"^\s*so[\s,]+(?:you|the|this|that)\b", re.IGNORECASE),
]
_FORBIDDEN_ANYWHERE = [
    re.compile(r"\bjust to confirm\b", re.IGNORECASE),
    re.compile(r"\bcan i confirm\b", re.IGNORECASE),
    re.compile(r"\bi['’]?d like to ask\b", re.IGNORECASE),
    re.compile(r"\bi have a few quick questions\b", re.IGNORECASE),
    re.compile(r"\bnow i['’]?d like to\b", re.IGNORECASE),
    re.compile(r"\blet me ask you\b", re.IGNORECASE),
    re.compile(r"\bmoving on to\b", re.IGNORECASE),
]

# Synthetic patient turns. Realistic enough to drive a real conversation,
# fixed so the test is deterministic-ish across runs (modulo the LLM's
# stochasticity). The patient gives a lot in turn 1 — a natural opener
# that gives Maria the chance to skip Phase-0 noise.
_PATIENT_TURNS = [
    "Yeah now's fine. This is Sarah Chen, I'm here for my hypothyroidism follow-up tomorrow at 2 PM.",
    "I've got a sharp pain in my right side, kind of low down. Started a couple days ago.",
    "Tuesday morning, while I was making coffee.",
    "Worse when I press on it, and after eating heavy meals. Lying still helps a bit.",
    "Right now maybe a four. Last night it was a seven.",
    "No fever, no chills. A bit nauseous yesterday. No throwing up.",
]

# Use the live GREETING constant from app.chat so the test always seeds
# the conversation with whatever the web chat actually opens with — no
# silent drift between test fixture and production opener.
_GREETING = GREETING


@pytest.mark.requires_api_key
def test_live_conversation_avoids_script_phrases():
    """Drive a 6-turn synthetic conversation; lint Maria's actual replies.

    Three contracts are enforced on every agent reply:
      1. No turn opens with a stock acknowledgment ("Got it.", "Great.",
         "Perfect.", etc.).
      2. No turn contains a meta-narration phrase ("just to confirm",
         "can I confirm", "I'd like to ask").
      3. Word count per turn ≤ 30 (length-budget target is 12–25; 30 leaves
         a small margin for genuinely complex questions).

    Plus a global: the patient's first name appears in agent turns at most
    twice across the whole conversation (greeting + wrap-up — the prompt
    explicitly forbids name-spam).
    """
    # Seed the conversation with the canned greeting that /chat/start emits,
    # then alternate user turn → agent turn for the rest.
    history: list[ChatMessage] = [
        ChatMessage(role="assistant", content=_GREETING),
    ]
    agent_replies: list[str] = []

    for patient_text in _PATIENT_TURNS:
        history.append(ChatMessage(role="user", content=patient_text))
        try:
            reply = chat_turn(history, system=SYSTEM_PROMPT)
        except LLMQuotaExhausted as e:
            # All three Gemini models exhausted for the day. Not a content
            # regression — skip rather than fail. Daily limits reset at
            # Pacific midnight; per-minute limits clear in seconds.
            pytest.skip(f"Gemini daily quota exhausted across all fallback models: {e}")
        history.append(ChatMessage(role="assistant", content=reply))
        agent_replies.append(reply)

    # 1. No stock acknowledgment at the start of any turn.
    for i, reply in enumerate(agent_replies):
        for pat in _FORBIDDEN_TURN_START:
            assert not pat.match(reply), (
                f"Turn {i + 1} starts with a stock acknowledgment: {reply!r}\n"
                f"Pattern matched: {pat.pattern!r}"
            )

    # 2. No meta-narration anywhere.
    for i, reply in enumerate(agent_replies):
        for pat in _FORBIDDEN_ANYWHERE:
            assert not pat.search(reply), (
                f"Turn {i + 1} contains meta-narration: {reply!r}\n"
                f"Pattern matched: {pat.pattern!r}"
            )

    # 3. Length budget. A short reply is the whole point of the rewrite.
    for i, reply in enumerate(agent_replies):
        # Strip trailing punctuation/whitespace, count whitespace-separated tokens.
        word_count = len(reply.split())
        assert word_count <= 30, (
            f"Turn {i + 1} is {word_count} words (budget: ≤ 30): {reply!r}"
        )

    # Global: name-spam check. "Sarah" should appear in agent text at most
    # twice — the prompt allows greeting + goodbye — and we never hit the
    # goodbye in this 6-turn slice, so realistically ≤ 1 in this window.
    name_hits = sum(
        len(re.findall(r"\bSarah\b", reply, flags=re.IGNORECASE))
        for reply in agent_replies
    )
    assert name_hits <= 2, (
        f"Maria mentioned the patient's name {name_hits} times across "
        f"{len(agent_replies)} turns; budget is ≤ 2. Replies:\n"
        + "\n".join(f"  {i + 1}. {r!r}" for i, r in enumerate(agent_replies))
    )

    # 4. No-repeat-question check. The founder's real call had:
    #    "Are you taking any medications right now?" / "No." /
    #    "Are you taking any medications right now?" / "I said no."
    # If two consecutive agent turns share the same first 6 words, the
    # agent is likely re-asking after STT garbled the answer — annoying,
    # patronizing, and the prompt forbids it.
    def _first_words(s: str, n: int = 6) -> str:
        toks = re.findall(r"\w+", s.lower())
        return " ".join(toks[:n])

    for i in range(1, len(agent_replies)):
        prev = _first_words(agent_replies[i - 1])
        curr = _first_words(agent_replies[i])
        # Allow exact repeats only if the previous reply was very short
        # (e.g. "And your name?" might legitimately repeat after a no-op
        # patient turn). Otherwise flag.
        if prev and curr and prev == curr and len(agent_replies[i - 1].split()) > 3:
            raise AssertionError(
                f"Agent repeated question between turns {i} and {i + 1} "
                f"(first 6 words match):\n"
                f"  Turn {i}: {agent_replies[i - 1]!r}\n"
                f"  Turn {i + 1}: {agent_replies[i]!r}"
            )
