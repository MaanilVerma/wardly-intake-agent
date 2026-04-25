/* Wardly Pre-Visit Intake — front-end chat controller.
 *
 * Talks to /chat/start, /chat/message, /chat/finalize. Optional voice in/out
 * via the browser-native Web Speech API (Chrome/Edge). Typing always works.
 */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const chatEl = $("chat");
  const statusEl = $("status");
  const composer = $("composer");
  const textInput = $("text-input");
  const sendBtn = $("send");
  const micBtn = $("mic");
  const startBtn = $("start");
  const endBtn = $("end");
  const autoSpeakToggle = $("auto-speak");

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const SUPPORTS_STT = Boolean(SR);
  const SUPPORTS_TTS = "speechSynthesis" in window;

  let sessionId = null;
  let isListening = false;
  let isThinking = false;
  let isFinalizing = false;
  let recognition = null;

  // ---------- UI helpers ----------

  function setStatus(text, kind = "") {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " " + kind : "");
  }

  function appendMessage(role, content) {
    // Clear the welcome card on the first real message.
    const welcome = chatEl.querySelector(".welcome");
    if (welcome) welcome.remove();

    const wrap = document.createElement("div");
    wrap.className = "msg " + (role === "agent" ? "agent" : "patient");
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "agent" ? "Agent" : "You";
    const body = document.createElement("span");
    body.textContent = content;
    wrap.appendChild(who);
    wrap.appendChild(body);
    chatEl.appendChild(wrap);
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function setComposerEnabled(enabled) {
    textInput.disabled = !enabled;
    sendBtn.disabled = !enabled;
    micBtn.disabled = !enabled || !SUPPORTS_STT;
    if (enabled) textInput.focus();
  }

  function setBusy(busy) {
    isThinking = busy;
    if (busy) {
      setStatus("Agent is typing…", "thinking");
      sendBtn.disabled = true;
      micBtn.disabled = true;
      textInput.disabled = true;
    } else {
      setStatus("");
      setComposerEnabled(Boolean(sessionId) && !isFinalizing);
    }
  }

  // ---------- TTS ----------

  function speak(text) {
    if (!SUPPORTS_TTS || !autoSpeakToggle.checked) return;
    try {
      // Cancel any pending speech before starting a new utterance to avoid
      // overlap when the user advances the conversation quickly.
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

  // ---------- STT ----------

  function buildRecognition() {
    if (!SUPPORTS_STT) return null;
    const r = new SR();
    r.lang = "en-US";
    r.continuous = false;
    r.interimResults = false;
    r.maxAlternatives = 1;

    r.onstart = () => {
      isListening = true;
      micBtn.classList.add("listening");
      setStatus("Listening… speak when ready", "listening");
    };
    r.onerror = (ev) => {
      console.warn("STT error", ev);
      setStatus("Voice input error: " + (ev.error || "unknown") + ". You can type instead.", "error");
    };
    r.onend = () => {
      isListening = false;
      micBtn.classList.remove("listening");
      if (statusEl.classList.contains("listening")) setStatus("");
    };
    r.onresult = (ev) => {
      const transcript = ev.results[0][0].transcript.trim();
      if (transcript) {
        textInput.value = transcript;
        // Submit immediately — the user clearly intends to send what they said.
        composer.requestSubmit();
      }
    };
    return r;
  }

  function toggleListening() {
    if (!recognition) return;
    if (isListening) {
      recognition.stop();
    } else {
      // Pause TTS while we listen so the agent doesn't talk over the patient.
      if (SUPPORTS_TTS) window.speechSynthesis.cancel();
      try {
        recognition.start();
      } catch (e) {
        console.warn("recognition.start() threw", e);
      }
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
    setStatus("Starting…");
    try {
      const data = await postJSON("/chat/start", {});
      sessionId = data.session_id;
      appendMessage("agent", data.first_message);
      speak(data.first_message);
      setComposerEnabled(true);
      endBtn.disabled = false;
      textInput.placeholder = "Type your reply, or click the mic…";
      setStatus("");
    } catch (e) {
      console.error(e);
      setStatus("Couldn't start the session. Check the server logs.", "error");
      startBtn.disabled = false;
    }
  }

  async function sendMessage(content) {
    if (!sessionId || !content.trim()) return;
    appendMessage("patient", content);
    textInput.value = "";
    setBusy(true);
    try {
      const data = await postJSON("/chat/message", {
        session_id: sessionId,
        content,
      });
      appendMessage("agent", data.assistant_message);
      speak(data.assistant_message);
    } catch (e) {
      console.error(e);
      setStatus("Message failed. Check the server logs.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function finalizeSession() {
    if (!sessionId || isFinalizing) return;
    if (!confirm("End the intake and generate the brief?")) return;
    isFinalizing = true;
    endBtn.disabled = true;
    startBtn.disabled = true;
    setComposerEnabled(false);
    setStatus("Generating clinical brief… this can take a few seconds.", "thinking");
    if (SUPPORTS_TTS) window.speechSynthesis.cancel();
    try {
      const data = await postJSON("/chat/finalize", { session_id: sessionId });
      window.location.href = data.brief_url;
    } catch (e) {
      console.error(e);
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

  micBtn.addEventListener("click", () => {
    if (isThinking || !sessionId) return;
    toggleListening();
  });

  startBtn.addEventListener("click", startSession);
  endBtn.addEventListener("click", finalizeSession);

  // Initial state
  if (!SUPPORTS_STT) {
    micBtn.title = "Voice input requires Chrome or Edge — type instead";
    micBtn.classList.add("disabled");
  } else {
    recognition = buildRecognition();
  }
  if (!SUPPORTS_TTS) {
    autoSpeakToggle.checked = false;
    autoSpeakToggle.disabled = true;
    autoSpeakToggle.parentElement.title = "Speech synthesis not supported in this browser";
  }
})();
