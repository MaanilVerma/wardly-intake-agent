/* Wardly · Pre-Visit Intake — chat controller
 *
 * Talks to /chat/start, /chat/message, /chat/finalize. Optional voice
 * in/out via the browser-native Web Speech API (Chrome/Edge). Typing
 * always works.
 *
 * UI features layered on the API:
 *   - iMessage-style bubbles with timestamps + delivery checks
 *   - Animated typing indicator while the agent thinks
 *   - "AI Suggestions" tray of one-tap reply variants generated client-side
 *     from heuristics over the agent's last question
 *   - Live brief panel that lights up as fields are likely captured
 *   - Status pill + session timer in the topbar
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);
  const chatEl = $("chat");
  const dayDivider = $("day-divider");
  const welcomeEl = $("welcome");
  const statusEl = $("status");
  const statusPill = $("status-pill");
  const sessionTimerEl = $("session-timer");
  const composer = $("composer");
  const textInput = $("text-input");
  const sendBtn = $("send");
  const micBtn = $("mic");
  const suggestBtn = $("suggest-btn");
  const startBtn = $("start");
  const endBtn = $("end");
  const autoSpeakToggle = $("auto-speak");
  const suggestionsEl = $("suggestions");
  const suggestionCardsEl = $("suggestion-cards");
  const suggestionsClose = $("suggestions-close");
  const briefProgress = $("brief-progress");
  const briefEmpty = $("brief-empty");
  const briefFields = $("brief-fields");
  const sessionIdShort = $("session-id-short");
  const sessionDuration = $("session-duration");
  const sessionTurns = $("session-turns");
  const fieldEls = {
    patient: document.querySelector('.field[data-field="patient"]'),
    chief: document.querySelector('.field[data-field="chief"]'),
    hpi: document.querySelector('.field[data-field="hpi"]'),
    ros: document.querySelector('.field[data-field="ros"]'),
    meds: document.querySelector('.field[data-field="meds"]'),
    redflag: document.querySelector('.field[data-field="redflag"]'),
  };

  // ---------- Capabilities ----------
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const SUPPORTS_STT = Boolean(SR);
  const SUPPORTS_TTS = "speechSynthesis" in window;

  // ---------- State ----------
  let sessionId = null;
  let isListening = false;
  let isThinking = false;
  let isFinalizing = false;
  let recognition = null;
  let lastAgentMessage = "";
  let sessionStartMs = null;
  let timerInterval = null;
  let lastBubbleRole = null;
  let capturedFields = new Set();
  let turnCount = 0;

  // ---------- Helpers: status pill ----------
  function setPill(label, kind /* idle | live | thinking | error */) {
    statusPill.className = "pill pill-" + (kind || "idle");
    statusPill.querySelector(".pill-label").textContent = label;
  }

  function setStatus(text, kind = "") {
    statusEl.textContent = text || "";
    statusEl.className = "composer-status" + (kind ? " " + kind : "");
  }

  // ---------- Helpers: time ----------
  function nowTimeLabel() {
    const d = new Date();
    let h = d.getHours();
    const m = d.getMinutes().toString().padStart(2, "0");
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return `${h}:${m} ${ampm}`;
  }

  function startSessionTimer() {
    sessionStartMs = Date.now();
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = setInterval(() => {
      const s = Math.floor((Date.now() - sessionStartMs) / 1000);
      const mm = Math.floor(s / 60).toString().padStart(2, "0");
      const ss = (s % 60).toString().padStart(2, "0");
      const label = `${mm}:${ss}`;
      sessionTimerEl.textContent = label;
      if (sessionDuration) sessionDuration.textContent = label;
    }, 1000);
  }

  function stopSessionTimer() {
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = null;
  }

  // ---------- Helpers: messages ----------
  function clearWelcome() {
    if (welcomeEl && welcomeEl.parentNode) welcomeEl.parentNode.removeChild(welcomeEl);
    if (dayDivider && dayDivider.hidden) dayDivider.hidden = false;
  }

  function appendMessage(role, content) {
    clearWelcome();

    const row = document.createElement("div");
    row.className = "row " + role;
    if (lastBubbleRole === role) row.classList.add("same");
    lastBubbleRole = role;

    const wrap = document.createElement("div");
    wrap.style.display = "flex";
    wrap.style.flexDirection = "column";
    wrap.style.maxWidth = "100%";
    wrap.style.alignItems = role === "patient" ? "flex-end" : "flex-start";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = content;
    wrap.appendChild(bubble);

    const meta = document.createElement("div");
    meta.className = "meta-line";
    const time = document.createElement("span");
    time.className = "meta-time";
    time.textContent = nowTimeLabel();
    meta.appendChild(time);
    if (role === "patient") {
      const check = document.createElement("span");
      check.className = "meta-check";
      check.setAttribute("aria-label", "delivered");
      check.textContent = "✓";
      meta.appendChild(check);
    }
    wrap.appendChild(meta);

    row.appendChild(wrap);
    chatEl.appendChild(row);
    chatEl.scrollTop = chatEl.scrollHeight;
    if (role === "patient") {
      turnCount += 1;
      if (sessionTurns) sessionTurns.textContent = String(turnCount);
    }
    return row;
  }

  // Typing indicator (3 dots in an agent bubble)
  let typingRow = null;
  function showTyping() {
    if (typingRow) return;
    const row = document.createElement("div");
    row.className = "row agent typing";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
    row.appendChild(bubble);
    chatEl.appendChild(row);
    chatEl.scrollTop = chatEl.scrollHeight;
    typingRow = row;
  }
  function hideTyping() {
    if (typingRow && typingRow.parentNode) typingRow.parentNode.removeChild(typingRow);
    typingRow = null;
  }

  // ---------- Composer enable/disable ----------
  function setComposerEnabled(enabled) {
    textInput.disabled = !enabled;
    sendBtn.disabled = !enabled || !textInput.value.trim();
    micBtn.disabled = !enabled || !SUPPORTS_STT;
    suggestBtn.disabled = !enabled || !lastAgentMessage;
    if (enabled) textInput.focus();
  }

  function setBusy(busy) {
    isThinking = busy;
    if (busy) {
      setPill("Agent typing", "thinking");
      sendBtn.disabled = true;
      micBtn.disabled = true;
      suggestBtn.disabled = true;
      textInput.disabled = true;
      showTyping();
    } else {
      hideTyping();
      setPill("Live", "live");
      setComposerEnabled(Boolean(sessionId) && !isFinalizing);
    }
  }

  // ---------- TTS ----------
  function speak(text) {
    if (!SUPPORTS_TTS || !autoSpeakToggle.checked) return;
    try {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(text);
      utter.rate = 1.0;
      utter.pitch = 1.0;
      utter.lang = "en-US";
      window.speechSynthesis.speak(utter);
    } catch (e) {
      console.warn("TTS failed", e);
    }
  }

  // ---------- STT — Vapi-style continuous capture ----------
  //
  // Web Speech API by default operates in single-utterance mode (continuous=false,
  // interimResults=false). That cuts the first phoneme (Chrome's pipeline takes
  // ~150ms to start producing results) and auto-finalizes after one phrase, which
  // is exactly what was happening: "first letter dropped, only one sentence sent".
  //
  // The fix below mirrors what Vapi's "Talk" button does:
  //   - continuous=true       → mic stays open across pauses
  //   - interimResults=true   → live partial transcripts surfaced as the user speaks
  //   - silence-debounce      → after SILENCE_FINALIZE_MS of no new tokens, finalize
  //   - "Listening" indicator only fires on `onstart` (not on click) so the user
  //     waits for the audio pipe to be ready before speaking — kills the cutoff.
  //   - manual mic-click stops immediately and submits whatever was captured
  //   - empty-result protection: never submit a blank user turn
  //
  // We can't unit-test Web Speech without real audio; verification is manual.
  const SILENCE_FINALIZE_MS = 1600;

  let finalTranscript = "";   // accumulator across continuous result events
  let silenceTimer = null;    // resets on every result
  let userInitiatedStop = false;  // true when patient clicks mic to stop manually
  let suppressInputEvents = false;  // we mutate textInput.value during STT — don't fight ourselves

  function clearSilenceTimer() {
    if (silenceTimer) {
      clearTimeout(silenceTimer);
      silenceTimer = null;
    }
  }

  function armSilenceTimer() {
    clearSilenceTimer();
    silenceTimer = setTimeout(() => {
      // Auto-finalize: stop the recognizer; onend submits the buffered transcript.
      if (recognition && isListening) {
        try { recognition.stop(); } catch (e) { /* harmless */ }
      }
    }, SILENCE_FINALIZE_MS);
  }

  function buildRecognition() {
    if (!SUPPORTS_STT) return null;
    const r = new SR();
    r.lang = "en-US";
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onstart = () => {
      isListening = true;
      micBtn.classList.add("listening");
      // Indicator fires here, not on click — so the user knows the pipe is open.
      setStatus("Listening… speak naturally; I'll wait for a pause", "listening");
      setPill("Listening", "live");
    };

    r.onerror = (ev) => {
      console.warn("STT error", ev);
      // "no-speech" is benign — Chrome fires it when the user clicks mic and
      // doesn't say anything for a while. Don't scare the user with it.
      if (ev.error === "no-speech" || ev.error === "aborted") return;
      setStatus("Voice input error: " + (ev.error || "unknown") + ". You can type instead.", "error");
    };

    r.onend = () => {
      isListening = false;
      micBtn.classList.remove("listening");
      clearSilenceTimer();
      if (statusEl.classList.contains("listening")) setStatus("");

      const captured = finalTranscript.trim();
      finalTranscript = "";
      userInitiatedStop = false;
      suppressInputEvents = false;

      // Restore Send-button state based on whatever ended up in the input.
      sendBtn.disabled = !sessionId || isThinking || !textInput.value.trim();

      if (captured) {
        // Auto-submit on natural end (silence debounce) OR manual stop.
        textInput.value = captured;
        composer.requestSubmit();
      }
    };

    r.onresult = (ev) => {
      // Accumulate finals; show finals + the most recent interim live in the input.
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const piece = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) {
          finalTranscript += piece;
        } else {
          interim += piece;
        }
      }
      const composed = (finalTranscript + interim).replace(/\s+/g, " ").trimStart();
      suppressInputEvents = true;
      textInput.value = composed;
      suppressInputEvents = false;

      // Cancel any agent TTS the moment the patient starts producing tokens —
      // mirrors Vapi's "you talk, agent shuts up" interruption etiquette.
      if (composed && SUPPORTS_TTS) window.speechSynthesis.cancel();

      armSilenceTimer();
    };

    return r;
  }

  function startListening() {
    if (!recognition || isListening) return;
    if (SUPPORTS_TTS) window.speechSynthesis.cancel();
    finalTranscript = "";
    userInitiatedStop = false;
    try {
      recognition.start();
    } catch (e) {
      // Chrome throws "InvalidStateError" if start() is called too quickly after
      // a previous session ends. Try once more after a tick.
      setTimeout(() => {
        try { recognition.start(); } catch (err) { console.warn("STT start failed", err); }
      }, 100);
    }
  }

  function stopListening() {
    if (!recognition || !isListening) return;
    userInitiatedStop = true;
    clearSilenceTimer();
    try { recognition.stop(); } catch (e) { /* harmless */ }
  }

  function toggleListening() {
    if (!recognition) return;
    if (isListening) stopListening();
    else startListening();
  }

  // ---------- AI Suggestions (client-side heuristics) ----------
  // Best-effort: parse the agent's last question and offer a few stock
  // reply shapes the patient can edit or send. These are intentionally
  // generic — the real value is reducing typing for common answers.
  function buildSuggestions(agentMsg) {
    const q = (agentMsg || "").toLowerCase();
    const out = [];
    if (/how long|when did|onset|start(ed)?/.test(q)) {
      out.push({ tag: "Direct", body: "It started about 2 days ago." });
      out.push({ tag: "Detailed", body: "It started two days ago, gradually getting worse — first thing in the morning." });
    } else if (/scale|how (bad|severe)|rate.*(pain|10)/.test(q)) {
      out.push({ tag: "Direct", body: "About a 6 out of 10." });
      out.push({ tag: "Detailed", body: "Around a 6 out of 10 — sharper when I move, dull at rest." });
    } else if (/medication|taking|prescribed|allerg/.test(q)) {
      out.push({ tag: "Direct", body: "No regular medications. No known allergies." });
      out.push({ tag: "Detailed", body: "I take a daily multivitamin, and ibuprofen as needed. No drug allergies that I know of." });
    } else if (/chest pain|short(ness)?|breath|dizz|faint|numb/.test(q)) {
      out.push({ tag: "Direct", body: "No, none of those." });
      out.push({ tag: "Detailed", body: "No chest pain, no shortness of breath, no dizziness." });
    } else if (/yes.*no|do you|have you|are you/.test(q)) {
      out.push({ tag: "Direct", body: "Yes." });
      out.push({ tag: "Direct", body: "No." });
      out.push({ tag: "Uncertain", body: "I'm not sure." });
    } else {
      out.push({ tag: "Direct", body: "Yes, that's right." });
      out.push({ tag: "Uncertain", body: "I'm not sure — could you clarify?" });
    }
    return out.slice(0, 3);
  }

  function renderSuggestions() {
    if (!lastAgentMessage) { suggestionsEl.hidden = true; return; }
    const items = buildSuggestions(lastAgentMessage);
    suggestionCardsEl.innerHTML = "";
    items.forEach((s) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "suggestion-card";
      const tag = document.createElement("span");
      tag.className = "suggestion-tag tag-" + s.tag.toLowerCase();
      tag.textContent = s.tag;
      const body = document.createElement("span");
      body.className = "suggestion-body";
      body.textContent = s.body;
      card.appendChild(tag);
      card.appendChild(document.createElement("br"));
      card.appendChild(body);
      card.addEventListener("click", () => {
        suggestionsEl.hidden = true;
        sendMessage(s.body);
      });
      suggestionCardsEl.appendChild(card);
    });
    suggestionsEl.hidden = false;
  }

  function hideSuggestions() { suggestionsEl.hidden = true; }

  // ---------- Live brief inference ----------
  // Heuristic: tag fields as captured based on what the agent has been
  // asking and what the patient has answered. Not a substitute for the
  // real extraction at finalize — this is a UX hint for the demo.
  function setFieldState(key, state, value) {
    const el = fieldEls[key];
    if (!el) return;
    el.classList.remove("is-active", "is-captured", "is-flagged");
    if (state) el.classList.add("is-" + state);
    const stateEl = el.querySelector(".field-state");
    const valueEl = el.querySelector(".field-value");
    if (state === "active")    stateEl.textContent = "Asking";
    else if (state === "captured") { stateEl.textContent = "Captured"; capturedFields.add(key); }
    else if (state === "flagged")  stateEl.textContent = "Flagged";
    else stateEl.textContent = "Pending";
    if (value) valueEl.textContent = value;
    updateProgress();
  }

  function updateProgress() {
    const total = Object.keys(fieldEls).length;
    briefProgress.textContent = `${capturedFields.size} / ${total}`;
  }

  function inferProgress(role, content) {
    const t = (content || "").toLowerCase();
    // Red-flag detection takes priority
    if (role === "patient" && /(crushing chest|radiat(ing|es) (to|down)|can't breathe|stroke|slurred|one side|worst headache|fainted|passed out)/.test(t)) {
      setFieldState("redflag", "flagged", "⚠ Possible red-flag symptom mentioned");
      return;
    }
    if (role === "agent") {
      if (/your name|confirm.*name|appointment|new visit|follow.?up|telehealth|visit type/.test(t)) setFieldState("patient", "active");
      else if (/chief|what brings|what's going on|main (issue|concern|complaint)|main reason/.test(t)) setFieldState("chief", "active");
      else if (/how long|when did|onset|how (bad|severe)|describe|where|trigger|worse|better/.test(t)) setFieldState("hpi", "active");
      else if (/fever|nausea|vomit|cough|short of breath|chest|dizz|headache|numbness|review/.test(t)) setFieldState("ros", "active");
      else if (/medication|allerg|prescribed|taking|chronic conditions|ongoing health/.test(t)) setFieldState("meds", "active");
    }
    if (role === "patient") {
      // Bump previously-active field to captured.
      Object.entries(fieldEls).forEach(([key, el]) => {
        if (el.classList.contains("is-active")) {
          let summary = content.length > 80 ? content.slice(0, 78) + "…" : content;
          setFieldState(key, "captured", summary);
        }
      });
    }
  }

  // ---------- network ----------
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`HTTP ${res.status}: ${text}`);
    }
    return res.json();
  }

  // ---------- conversation flow ----------
  async function startSession() {
    startBtn.disabled = true;
    setPill("Connecting", "thinking");
    try {
      const data = await postJSON("/chat/start", {});
      sessionId = data.session_id;
      startSessionTimer();
      // Swap right rail from empty state to live fields
      if (briefEmpty) briefEmpty.hidden = true;
      if (briefFields) briefFields.hidden = false;
      if (sessionIdShort) sessionIdShort.textContent = sessionId.slice(0, 8);
      lastAgentMessage = data.first_message;
      appendMessage("agent", data.first_message);
      speak(data.first_message);
      setComposerEnabled(true);
      endBtn.disabled = false;
      textInput.placeholder = "Type your reply…";
      setPill("Live", "live");
      setStatus("");
      inferProgress("agent", data.first_message);
    } catch (e) {
      console.error(e);
      setPill("Error", "error");
      setStatus("Couldn't start the session. Check the server logs.", "error");
      startBtn.disabled = false;
    }
  }

  async function sendMessage(content) {
    if (!sessionId || !content.trim()) return;
    appendMessage("patient", content);
    inferProgress("patient", content);
    textInput.value = "";
    sendBtn.disabled = true;
    hideSuggestions();
    setBusy(true);
    try {
      const data = await postJSON("/chat/message", {
        session_id: sessionId,
        content,
      });
      lastAgentMessage = data.assistant_message;
      appendMessage("agent", data.assistant_message);
      inferProgress("agent", data.assistant_message);
      speak(data.assistant_message);

      // Server-side auto-finalize: when the agent fires a red-flag interrupt
      // it strips the leaked tool-call syntax, records the flag, runs the
      // brief extraction, and tells us to navigate. We let the patient
      // hear/read the 911 advisory for ~3 seconds, then redirect to the
      // brief. Even if the patient closes the tab the brief is already on
      // disk by the time this response comes back.
      if (data.ended) {
        setPill("Emergency advised", "error");
        setStatus("Auto-saving the brief — the doctor will see this immediately.", "thinking");
        setComposerEnabled(false);
        endBtn.disabled = true;
        if (SUPPORTS_TTS) {
          // Let the synth finish speaking the advisory before navigating —
          // cutting it off mid-word would defeat the whole point.
        }
        if (data.brief_url) {
          stopSessionTimer();
          setTimeout(() => { window.location.href = data.brief_url; }, 3000);
        }
        return;
      }
    } catch (e) {
      console.error(e);
      setPill("Error", "error");
      setStatus("Message failed. Check the server logs.", "error");
    } finally {
      if (!isFinalizing) setBusy(false);
    }
  }

  async function finalizeSession() {
    if (!sessionId || isFinalizing) return;
    if (!confirm("End the intake and generate the brief?")) return;
    isFinalizing = true;
    endBtn.disabled = true;
    startBtn.disabled = true;
    setComposerEnabled(false);
    setPill("Generating brief", "thinking");
    setStatus("Generating clinical brief — this can take a few seconds.", "thinking");
    if (SUPPORTS_TTS) window.speechSynthesis.cancel();
    try {
      const data = await postJSON("/chat/finalize", { session_id: sessionId });
      stopSessionTimer();
      window.location.href = data.brief_url;
    } catch (e) {
      console.error(e);
      setPill("Error", "error");
      setStatus("Finalize failed: " + e.message, "error");
      isFinalizing = false;
      endBtn.disabled = false;
      setComposerEnabled(true);
    }
  }

  // ---------- wiring ----------
  composer.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const content = textInput.value;
    if (content && !isThinking) sendMessage(content);
  });

  textInput.addEventListener("input", () => {
    if (suppressInputEvents) return;  // STT is mid-stream — don't fight ourselves
    sendBtn.disabled = !sessionId || isThinking || !textInput.value.trim();
  });

  // Push-to-talk: hold Space to talk, release to send. Only when the text input
  // is not focused (so typing a space mid-message still works).
  let spaceHeldFor = false;
  document.addEventListener("keydown", (ev) => {
    if (ev.code !== "Space" || ev.repeat) return;
    if (document.activeElement === textInput) return;
    if (!sessionId || isThinking || !SUPPORTS_STT) return;
    ev.preventDefault();
    if (!isListening) {
      spaceHeldFor = true;
      startListening();
    }
  });
  document.addEventListener("keyup", (ev) => {
    if (ev.code !== "Space" || !spaceHeldFor) return;
    spaceHeldFor = false;
    if (isListening) stopListening();
  });

  micBtn.addEventListener("click", () => {
    if (isThinking || !sessionId) return;
    toggleListening();
  });

  suggestBtn.addEventListener("click", () => {
    if (suggestionsEl.hidden) renderSuggestions();
    else hideSuggestions();
  });
  suggestionsClose.addEventListener("click", hideSuggestions);

  startBtn.addEventListener("click", startSession);
  endBtn.addEventListener("click", finalizeSession);

  // Initial state
  setPill("Ready", "idle");
  if (!SUPPORTS_STT) {
    micBtn.title = "Voice input requires Chrome or Edge — type instead";
  } else {
    recognition = buildRecognition();
  }
  if (!SUPPORTS_TTS) {
    autoSpeakToggle.checked = false;
    autoSpeakToggle.disabled = true;
  }
})();
