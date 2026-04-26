# Pre-Visit Clinical Intake Agent

A voice + chat agent that conducts a pre-visit clinical intake with a patient and produces a structured clinical brief — chief complaint, history of present illness (OPQRST), targeted review of systems, quick history, and red-flag detection — that a clinician can read in 30 seconds before walking into the visit.

Two transports, one brain. Web chat with browser-native voice as the primary path. Real phone calls via Vapi as the bonus path. Same model, same prompt, same JSON schema for both. The brief lands as JSON + Markdown on disk and as a chart-style HTML view at `/briefs/<call_id>`.

> **Not a medical device.** This is a technical demonstration. It is not certified for clinical use, has no PHI safeguards beyond gitignored local storage, and is not covered under any HIPAA Business Associate Agreement. See `LICENSE` for the full disclaimer.

---

## Demo

📹 **Loom walkthrough**: _<paste link after recording>_
🌐 **Web demo**: run locally per the **Quickstart** below, or deploy to any free Python host.

---

## What it does

1. Patient opens the web chat (or dials the Vapi phone number) and starts an intake.
2. The agent — a triage-nurse persona named **Maria** — runs a structured five-phase conversation:
   - **Verification** — name, appointment, visit type
   - **Chief complaint** — the main reason, in the patient's verbatim words
   - **HPI** via OPQRST (onset, provocation/palliation, quality, radiation, severity, timing) plus associated symptoms, context, prior episodes, treatments tried
   - **Targeted ROS** — Constitutional always, plus 2–3 systems mapped to the chief complaint, with both pertinent positives and negatives
   - **Quick history** — chronic conditions, current medications, drug allergies (one question per area, no probing)
3. If the patient describes a **red-flag symptom** (chest pain with radiation, stroke signs, anaphylaxis, suicidal ideation, active heavy bleeding, etc.), the agent interrupts the intake, speaks a 911 advisory, calls a `flag_red_flag` tool, and ends the call decisively.
4. At end-of-intake, the backend extracts a **JSON-Schema-validated brief** from the transcript using **Google Gemini** with a four-model fallback chain. The brief includes a deterministic **completeness score** (computed in Python — never trusted from the LLM) and optionally surfaces hedged **differential considerations**, **suggested follow-up questions**, and **candidate ICD-10 codes**, all clearly labeled "Not a diagnosis · For clinician review".
5. Artifacts land at `briefs/<call_id>.{json,md,transcript.txt}`. The clinician opens `/briefs/<call_id>` for the chart-style HTML view, or `/briefs/` for the index of all briefs sorted newest first.

---

## Quickstart

Three commands and one API key.

```bash
# 1. Clone and set up the environment
git clone <this-repo> wardly-intake-agent && cd wardly-intake-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure your free Google AI Studio API key
cp .env.example .env
# Edit .env and set GOOGLE_API_KEY (get one free at https://aistudio.google.com/apikey)

# 3. Run
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>, click **Start intake**, talk to the agent, click **End intake**, and you'll land on the rendered brief at `/briefs/<call_id>`. View all briefs at <http://localhost:8000/briefs/>.

For browser voice in/out, use **Chrome** or **Edge** — the Web Speech API isn't supported in Firefox or Safari (they fall back to typing automatically).

### Run the tests

```bash
pytest -v                     # 18 unit + static lint tests, no API call
GOOGLE_API_KEY=... pytest -v  # +3 eval tests + 1 live conversation lint
```

Tests that hit the model are gated on `GOOGLE_API_KEY` and skip cleanly without it.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| LLM | **Google Gemini** via Google AI Studio (free tier) — defaults to `gemini-3.1-flash-lite-preview` (500 RPD), with a fallback chain through `gemini-3-flash`, `gemini-2.5-flash-lite`, and `gemini-2.5-flash`. | Native JSON-Schema-constrained output. Generous free-tier quota. Provider-supported by Vapi, so the same model brand drives both transports. The fallback chain keeps the demo working even when one model's daily quota is saturated. |
| Browser voice | **Web Speech API** (`SpeechRecognition` + `SpeechSynthesis`) | 100% free, no signup, no telephony provider. Continuous listening with interim transcripts and silence-debounced auto-submit. Push-to-talk on Spacebar. Typing fallback for non-Chromium browsers. |
| Phone voice (optional) | **Vapi** — assistant config in `vapi/assistant_config.json` | Free US number on the trial credit, supports Google Gemini natively, and the in-dashboard "Talk to Assistant" web call works for users outside the US. |
| Backend | **FastAPI** + uvicorn, **Pydantic v2** | Async route handlers, Pydantic-native payload validation, ~1k lines for chat + webhooks + brief renderer + storage + index page. |
| Brief generation | **Single owned extraction path** — Gemini + JSON Schema, validated by Pydantic, completeness scored in Python | We deliberately do *not* depend on Vapi's structured-output feature. One reliable code path serves both transports. |
| Storage | **JSON + Markdown files on disk** under `briefs/` (gitignored) | Atomic writes (temp file + rename). No DB, no auth, no PHI to cloud. |

---

## Architecture

```
   ┌─────────────────────────┐                ┌──────────────────────────┐
   │  Browser (primary)      │                │  Vapi voice (optional)   │
   │  • Chat UI              │                │  • Free US number        │
   │  • Web Speech API       │                │  • Deepgram STT          │
   │  • Live brief panel     │                │  • ElevenLabs TTS        │
   │  • Push-to-talk + auto- │                │  • Same Gemini model     │
   │    submit on silence    │                │  • flag_red_flag tool    │
   └────────────┬────────────┘                └────────────┬─────────────┘
        /chat/* │                                          │ /webhook/*
                ▼                                          ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  FastAPI server (this repo)                                      │
   │  • Chat: /chat/start  /chat/message  /chat/finalize              │
   │  • Vapi: /webhook/tool-call (sync)  /webhook/end-of-call (async) │
   │  • View: /briefs/{call_id}  (chart-style HTML)                   │
   │  • List: /briefs/  (all briefs, newest first)                    │
   │  • Health: /healthz                                              │
   │                                                                  │
   │  Single shared extraction path                                   │
   │    app/brief.py:extract_from_transcript(transcript)              │
   │      → Gemini fallback chain                                     │
   │      → Pydantic validation                                       │
   │      → Computed completeness scoring                             │
   │      → Atomic write to briefs/<call_id>.{json,md,transcript.txt} │
   └──────────────────────────────────────────────────────────────────┘
```

---

## Project structure

```
prompts/
  system_prompt.md          # Live conversation prompt (Maria persona)
  brief_schema.json         # JSON Schema for the structured brief
app/
  main.py                   # FastAPI app, routes, briefs index
  models.py                 # Pydantic v2 models + completeness scoring
  prompts.py                # Loads system + extraction prompts at import
  llm.py                    # Gemini wrapper, model fallback chain, typed errors
  brief.py                  # extract_from_transcript() + recovery CLI
  chat.py                   # Web chat sessions + /chat/* routes
  webhooks.py               # Vapi /webhook/* handlers (signed secret optional)
  storage.py                # Atomic-write brief persistence + list_briefs()
  render.py                 # Chart-style HTML renderer + briefs index
  static/
    index.html              # Chat UI
    chat.js                 # Web Speech API + live brief panel
    style.css               # Design system
tests/
  test_models.py            # 14 unit tests (rendering + completeness math)
  test_prompt.py            # Static prompt lint + live conversation lint
  test_brief_extraction.py  # 3 eval tests against synthetic transcripts
  fixtures/                 # Three realistic intake transcripts
vapi/
  assistant_config.json     # Importable Vapi assistant configuration
briefs/                     # Generated artifacts (gitignored — simulated PHI)
```

---

## Configuration

Environment variables (set in `.env`):

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | yes | Google AI Studio API key. Free at <https://aistudio.google.com/apikey> — no credit card. |
| `VAPI_API_KEY` | optional | Only needed if scripting Vapi assistant creation. The Vapi dashboard works without it. |
| `VAPI_WEBHOOK_SECRET` | optional, **required for any non-local deployment** | Shared secret. When set, every Vapi webhook must carry the same value in `X-Vapi-Secret`. Unset = accept any caller (local dev only). |
| `LOG_LEVEL` | optional | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Defaults to `INFO`. |

---

## Setting up the Vapi voice path (optional)

Skip this if you only need the web chat.

1. **Create the assistant** in the [Vapi dashboard](https://dashboard.vapi.ai). Use `vapi/assistant_config.json` as the source of truth — copy each field into the matching dashboard control. Substitute:
   - `__SERVER_URL__` → your public HTTPS URL (ngrok for local dev, or your deployed hostname)
   - `__SYSTEM_PROMPT__` → the contents of `prompts/system_prompt.md`
2. **Create the `flag_red_flag` tool** under Tools → Create Tool. Type *Function*, **Async OFF**, server URL `<your-url>/webhook/tool-call`. Use the parameter schema in `vapi/assistant_config.json`.
3. **Confirm the assistant's Server Messages** include `end-of-call-report` and `status-update`. **Do not** include `tool-calls` — the tool's function-level URL is the route for those.
4. **Buy a free US number** under Phone Numbers → Buy → "Free Vapi Number" and attach the assistant. Or use the in-dashboard **Talk to Assistant** button for a WebRTC test call (works from any country).
5. **Do not enable Vapi's HIPAA mode** for this demo — it disables the artifact storage we need to extract the brief from.

For local dev, expose the FastAPI server with [ngrok](https://ngrok.com): `ngrok http 8000`, then plug the HTTPS URL from <http://127.0.0.1:4040> into the Vapi config above.

---

## Notable engineering choices

**Single owned extraction path.** Both transports (web chat and Vapi voice) converge on `extract_from_transcript()`. We deliberately do *not* depend on Vapi's structured-output feature — community reports show flakiness, and decoupling means a future swap to LiveKit / Twilio / a self-hosted voice stack changes only `app/webhooks.py`.

**Computed completeness, never LLM-reported.** The completeness score is calculated in Python from a weighted rubric over the populated fields. Anything the LLM emits in `brief.completeness` is dropped before validation. Don't trust the model on its own thoroughness.

**Model fallback chain.** Default `gemini-3.1-flash-lite-preview` (500 RPD on free tier as of April 2026), then `gemini-3-flash` → `gemini-2.5-flash-lite` → `gemini-2.5-flash`. A saturated daily quota on one model doesn't kill the demo. The predicate also catches *model not found* errors, so a stale preview name in the chain skips to the next instead of hard-failing.

**Defense-in-depth narrative scrubber.** Even with the extraction prompt forbidding placeholders, Gemini occasionally leaks `[AGE]-year-old female` into the HPI narrative. The brief module strips any `[ALL_CAPS]` token before validation and patches up the surrounding grammar. Real fix is the prompt; this is the safety net.

**Strict-order red-flag interrupt.** When the patient describes a red flag, the agent must (1) speak the 911 advisory verbatim, (2) call `flag_red_flag`, (3) call `end_call`. The order is enforced in the prompt with a banned-closings list — phrases like "thanks for the time" or "the doctor will have all of this" are forbidden after `flag_red_flag` fires. This came out of a real failed call where the agent fired the tool without speaking the advisory.

**Atomic writes everywhere.** `briefs/<id>.{json,md,transcript.txt}` are written through a temp file + rename. A clinician never sees a half-written brief.

**Recovery CLI.** `python -m app.brief briefs/<call_id>.transcript.txt` re-runs extraction on a saved transcript and writes the JSON + Markdown next to it. Useful when the live background extraction died mid-Loom (quota, network, anything).

**Static prompt lint + live conversation lint.** `tests/test_prompt.py` enforces (1) required section headers in the prompt, (2) behavioral rules paid for in real failed calls (each rule's presence pinned to a specific failure transcript), (3) a regression-tripwire size budget, and (4) a real-model conversation lint that runs a six-turn synthetic intake through `chat_turn()` and fails on stock acknowledgments, meta-narration, name-spam, and consecutive-question-repeat patterns.

**Live brief panel.** While the conversation runs, a side panel updates a six-field progress chip (Patient & visit / CC / HPI / ROS / Meds / Red flag). It's a UX cue, not a clinical record — the canonical brief is the one extracted at `/chat/finalize`.

---

## Roadmap

- **Gemma 3 / 4 as a deeper fallback layer.** Adds another ~16,000 RPD of free-tier headroom. Needs a separate code path because Gemma doesn't reliably honor `response_schema` — the implementation would emit JSON via prompt instruction, run JSON repair, and validate with Pydantic. ~60 lines.
- **Eval harness with LLM-as-judge.** The current eval tests assert clinical-structure invariants. A real product needs scored runs against ground-truth briefs across a corpus of synthetic personas with per-field accuracy and regression tracking.
- **Mid-call recovery.** When a patient revises ("actually it started Wednesday, not Tuesday") or jumps ahead ("oh and I have a rash too"), the agent should rebind cleanly without losing prior captures.
- **Webhook idempotency.** If Vapi retries `end-of-call-report` on a 5xx, dedupe by `call.id` and don't re-extract.
- **Persistent session store.** In-memory dict works for single-process demos; Redis (or similar) is the obvious next step for multi-worker deployments.
- **Multi-language.** The conversation prompt structure transfers cleanly; Spanish first.
- **Full PFSH** (past, family, social history). Current scope is the highest-yield slice; a full new-patient intake covers more.
- **HIPAA path.** Vapi BAA, encryption at rest, audit logging, no-disk PHI, redaction in non-clinical logs, access controls. The license disclaims clinical use until this lands.

---

## Honest tradeoffs

- I used **Vapi** rather than building the voice pipeline myself (LiveKit / Deepgram / ElevenLabs / Twilio). Faster to a working demo; less to show off engineering-wise. For a project where conversational design and clinical structure matter more than telephony plumbing, that's the right call.
- I rely on the **prompt** rather than a hard state machine to drive ROS adaptation, phase progression, and red-flag detection. A state machine would be more robust but more brittle to natural conversation. The prompt is paired with a deterministic completeness score and a pinned behavioral-rule lint as the safety net.
- The **AI Suggestions panel** (differential considerations + ICD-10 candidates) is unambiguously labeled "Not a diagnosis · For clinician review" and never spoken on the call. A clinical product that diagnosed during patient-facing conversation would create liability and undermine the clinician — the boundary is a product decision, not a model limitation.
- This is a **demonstration**, not a production medical system. See `LICENSE` for the full disclaimer.

---

## License

MIT. See [LICENSE](LICENSE).

The license includes an explicit disclaimer that this software is not a medical device, is not certified for clinical use, and is not covered under any HIPAA Business Associate Agreement. Do not use it on real patient data without first implementing PHI safeguards and obtaining the appropriate agreements with your model and telephony providers.
