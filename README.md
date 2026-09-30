# Leutheria

A voice-controlled AI assistant that controls your computer — opening apps, pulling live
data, and executing tasks via natural speech.

Architecture: a **Node/Electron shell** (`/app`) drives a **Python sidecar process**
(`/agent`) that does the actual agent work (STT, TTS, tool execution, LLM tool-calling).
The two talk over a local WebSocket.

See `plans/ROADMAP.md` (gitignored, local-only) for the current build plan.

## Architecture

```
Electron (app/)
---------------
main.js
  - spawns the Python agent, owns the WebSocket connection
  - event stream one way (chat/confirm/voice state), request/response the other
    (inventory reads, preference + voice-setting writes, matched by request_id)
  - hosts the activation overlay, the menu bar item and the Alt+Space shortcut
renderer/
  - Assistant     chat, push-to-talk mic, confirmation + skill-proposal cards, voice-state indicator
  - Library       every tool and skill: inspect, enable/disable, ask-first, delete
  - Activity      skill history + what actually gets used
  - Permissions   standing approvals and customized capabilities
  - Logs          the raw bridge stream
renderer/overlay/
  - floating, click-through "island" shown during a voice session; assets
    come from overlay/assets/manifest.json

        |
        |  ws://127.0.0.1:8765 -- JSON messages, both directions
        v

Python Agent (agent/agent/)
---------------------------
server.py
  - routes every message by type
  - logs everything to logs/events.jsonl

  "inventory" / "set_preference" / "delete_skill" / "revoke_trust"
    -> control.py -> inventory.py (catalog + usage + history)
                     preferences.py (enabled / ask-first, per capability)
                     trust.py, skills/registry.py
    answered inline -- never touches the LLM, never blocks a command

  voice_session.py (not a message -- runs on its own from startup)
    mic (sounddevice) -> openWakeWord "hey jarvis" -> Silero VAD endpointing
    -> stt.py (Whisper) -> dismissal? end : transcript text ----------+
    muted while tts.py is playing (half-duplex)                       |
                                                                      |
  "audio" message (push-to-talk)                                      |
    -> audio_input.py -> stt.py (Whisper) -> transcript text          |
                                                   |                  |
                        server.handle_transcript <-+------------------+
                        (a pending yes/no is answered here, else:)
                                                   |
  "text" (typed, or a transcript from above) <-----+
    |
    v
  agent_loop.py   (loop, up to 6 turns)
    |                                 ^
    v                                 | tool_result
  llm_anthropic.py (claude-opus-5) ---+
    |  tool_use
    v
  dispatcher.py
    resolves the name against SKILLS then TOOLS, and applies preferences.py:
    a disabled capability is refused outright, and "ask before running" can be
    turned on for anything (or off for a destructive tool) from the Library
    |                          |
    v                          v
  skills/registry.py      tools/registry.py
    setup_project            open_app
    (calls dispatch()        create_folder
     again for its own       list_files
     sub-steps)              run_command  (destructive)
                                  |
                                  v
                         scoped (GUI)? -> one prompt per target app, then that
                         app stays drivable for 15 minutes (session.py); some
                         apps are never drivable at all (ui_access.DENIED_APPS)

                         destructive? -> send "confirmation_required" AND
                         speak the prompt, pause until a "confirm" message
                         answers it -- which the next transcribed voice
                         reply can supply directly (voice_confirm.py)

  once agent_loop gets a final text reply (no more tool calls):
    |
    v
  tts.py (Piper, or ElevenLabs if selected) -> played through the speakers by the agent itself,
                    bracketed by "speaking" started/ended messages

  meanwhile, if the trace had >= 2 tool calls and no skill covers it yet
  (skill_learning.py, checked against logs/events.jsonl BEFORE this
  request logs its own entry):
    -> a past matching run exists: LLM diffs both into a confident definition
    -> first time seen: LLM generalizes from one example, flags anything
       uncertain as a clarifying question instead of guessing
    -> "skill_proposed" message -> Electron shows any questions + Save/Decline
    -> on approve: answers get baked in, written to skills/generated/*.json,
       live immediately
```

Key point the diagram is built around: **tools and skills look identical to Claude** — both
are just entries in the `tools` list sent with each request. `dispatcher.py` is the one
place that knows the difference, and a skill calling back into `dispatch()` for its own
sub-steps is why skills can't bypass the confirmation gate — a destructive step inside a
skill pauses exactly the same way a directly-called destructive tool would.

## Project structure

```
app/                  Electron shell
  main.js             Spawns the Python agent, connects to it over WebSocket, hosts the main
                       window, the voice overlay, the menu bar item and the global shortcut
  preload.js           Bridges the agent event stream + the management request/response calls
                       into the renderer safely (contextBridge, no node access in the page)
  renderer/
    index.html         Shell markup: sidebar, topbar, one <section> per view
    styles.css         Design tokens (dark + light) and every component style
    js/
      util.js          DOM helper, the inline SVG icon set, formatters, toasts
      app.js           Navigation, the shared inventory snapshot, theme, topbar wiring
      chat.js          Assistant view: transcript, confirmation + skill-proposal cards, mic
      library.js       Library view: master/detail over every tool and skill
      activity.js      Activity view: skill history timeline + usage stats
      permissions.js   Permissions view: standing approvals, customized capabilities
      logs.js          Logs view: the raw bridge stream
      voice.js         Header voice-state indicator (click to start/stop a session)
      voice-settings.js  Voice view: wake word, timeouts, dismiss phrases, input device
    overlay/
      overlay.html/.css/.js  The activation overlay (reuses styles.css tokens + util.js)
      assets/          Drop-in visuals: manifest.json + README.md for the asset designer
  scripts/
    mock-agent.js      Scripted stand-in agent (port 8799) for testing the Electron side

agent/                Python sidecar
  preferences.json     Per-capability enabled / ask-first overrides set from the Library
                       (gitignored -- local state, not source)
  SYSTEM_PROMPT.md     Persona/tone/behavior -- loaded once, passed as `system` on every
                       conversational LLM call. Hand-editable, no restart-the-world needed
                       beyond restarting the agent process.
  trusted_commands.json  Permanently-trusted (tool, args) pairs (gitignored -- local
                       state, not source). See trust.py below.
  voice_settings.json  Voice session settings set from the UI (gitignored -- local state)
  scripts/
    voice_sim.py       Drives the voice session with synthesized speech instead of the mic
                       and checks the state/message sequence per scenario
    voice_server_check.py  End-to-end: real server on a side port, a WAV as the mic,
                       the voice control messages, settings persistence
    skill_format_check.py  Generated-skill format: metadata, validation rules, load /
                       migrate / reject, placeholder substitution (temp dirs only)
    tts_check.py       TTS backends against a fake ElevenLabs server: request shape, PCM
                       decoding, every fallback to Piper, settings validation (no key needed)
  .venv/              Virtualenv (gitignored)
  agent/
    core/
      server.py        WebSocket server, message routing, the primary-connection registry
                       voice commands run on, and the one transcript path shared by
                       push-to-talk and the voice session (max_size=20MB -- a long
                       push-to-talk upload can exceed the 1MB library default, BUGS.md #7)
      voice_session.py  The always-listening state machine: wake word (openWakeWord), VAD
                        endpointing (Silero), Whisper, dismissal, silence timeout,
                        half-duplex muting while Leutheria speaks
      voice_settings.py Voice settings: defaults, validation, agent/voice_settings.json
      audio_io.py       Mic frames (sounddevice), the WAV stand-in source used in tests,
                        and speaker playback
      dispatcher.py     Routes {tool, args} payloads to tools and skills; refuses anything
                        disabled in preferences.py, then pauses for a live confirmation
                        round-trip when the capability asks first (destructive by default,
                        overridable either way from the Library) -- unless trust.py says
                        this exact (tool, args) pair is already trusted. The confirmation
                        prompt is also spoken via tts.py's speak()
      preferences.py    Per-capability `enabled` and `requires_confirmation` overrides,
                        persisted to agent/preferences.json. Read by dispatcher.py and by
                        both registries' schema builders, so a disabled capability is
                        never even described to the model
      control.py        Handles the UI's management messages (inventory / set_preference /
                        delete_skill / revoke_trust) inline in the read loop -- no LLM, no
                        blocking, answered with the caller's request_id
      inventory.py      One snapshot of everything the UI renders: the tool + skill catalog
                        with effective settings, per-capability usage counts scanned out of
                        logs/events.jsonl, the skill-lifecycle history, and standing trust
      trust.py          Session (in-memory) and permanent (trusted_commands.json) trust,
                        keyed on the exact (tool, args) pair -- never on tool name alone
      voice_confirm.py  interpret_yes_no(): word-boundary regex match of a transcribed
                        reply against approve/decline phrasings, for answering a pending
                        confirmation entirely by voice
      llm.py            LLMBackend interface (LLMTurn) -- swappable backend seam
      llm_anthropic.py  AnthropicBackend: calls claude-opus-5 with the tool schemas
      agent_loop.py     The Claude <-> tool-dispatch loop: keeps calling tools and feeding
                        results back until Claude gives a final answer (capped at 6 turns)
      audio_input.py    Decodes a base64 audio payload to a temp file, hands off to stt.py
      stt.py            Local Whisper ("base" model) transcription
      tts.py            Speaks replies: picks the TTS backend from the voice settings
                        (falling back to Piper if it fails) and plays the audio through
                        the Mac's speakers from the sidecar itself; speak() is shared by
                        server.py (final replies) and dispatcher.py (spoken confirmation
                        prompts)
      tts_backends.py   TTSBackend interface -- swappable engine seam, like llm.py.
                        PiperBackend (local, "en_US-amy-medium" by default) and
                        ElevenLabsBackend (cloud REST API, needs ELEVENLABS_API_KEY)
      skill_learning.py Checks logs/events.jsonl for a matching past run, then asks
                        Claude to generalize into a skill definition -- confidently
                        from two examples if repeated, or from one example (flagging
                        uncertain params as clarifying questions) if this is the first
      ui_access.py      macOS GUI primitives: app focus, Accessibility tree reads,
                        synthetic keystrokes. Holds the two structural mitigations the
                        scoped tier rests on -- DENIED_APPS and secure-field redaction --
                        plus workarounds for three silently-failing macOS behaviours
                        (all NSWorkspace state is frozen without a run-loop pump, AX
                        failing on Electron apps, CGEvent inheriting modifier flags).
                        All measured; see the docstrings before changing any of them
      session.py        Live session state: which app is focused, a rolling window of
                        plain conversation turns, and which apps are currently grantable
                        to the scoped tools. In-memory only -- grants never outlive the
                        process
      logging_util.py   Appends every message/decision/tool call to logs/events.jsonl
    tools/
      registry.py       TOOLS dict (fn, safety, description, input_schema) + anthropic_tool_schemas()
      open_app.py        create_folder.py    list_files.py       run_command.py
      get_clipboard.py   set_clipboard.py    open_url.py         show_notification.py
      take_screenshot.py read_file.py        append_text.py      trash_file.py
      move_file.py       copy_file.py        list_running_apps.py
      get_battery_status.py  get_disk_space.py  get_current_datetime.py
      focus_app.py       describe_ui.py      type_text.py        press_keys.py
                        One flat .py file per tool -- no per-tool folders/__init__.py;
                        that packaging turned out to be more annoying to navigate than
                        it was worth for single-function tools, so it was reverted.
    skills/
      registry.py       SKILLS dict + anthropic_skill_schemas(); loads (and validates,
                        and migrates) generated/*.json at startup, plus register_skill()
                        to add a new one at runtime
      validate.py       The generated-skill format contract: name/path rules, tools-only
                        steps, placeholder and JSON checks, content_hash + metadata
      template_skill.py The one reviewed interpreter every generated skill runs through --
                        a generated skill is data (steps + {param} placeholders), never
                        new code
      setup_project.py   First hand-written skill: create_folder + git init + open in
                        VS Code as one multi-step procedure, exposed to the LLM as a
                        single tool
      quick_note.py       append_text + show_notification: jot down a timestamped note
      backup_folder.py    copy_file with a generated timestamped destination name
      generated/         Agent-proposed, user-approved skills as JSON (gitignored --
                        local learned state, not source code)
    assets/voices/       Piper voice model (gitignored, auto-downloaded on first use)
  logs/                 events.jsonl (gitignored) -- see "Logs" section below
```

## Setup

Requires Homebrew Python 3.12 (the system/Xcode Python 3.9 is too old) and Node.

Also requires `ffmpeg` on the system (Whisper uses it to decode audio):

```bash
brew install ffmpeg
```

```bash
# Python side
cd agent
/opt/homebrew/bin/python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt   # includes openai-whisper (~1GB w/ torch) and piper-tts

# Node side
cd ../app
npm install
```

Controlling other apps additionally needs **Accessibility permission** (System Settings >
Privacy & Security > Accessibility) for whatever launched the agent — Electron under
`npm start`, or your terminal if you ran it standalone. See "Controlling other apps" below.

Both STT and TTS auto-download their models on first use, no manual steps needed:
- Whisper's `"base"` model (~140MB) goes to `~/.cache/whisper`.
- Piper's `"en_US-amy-medium"` voice (~60MB) goes to `agent/agent/assets/voices/` (gitignored).

Expect a one-time delay the first time each is actually used (first voice command in,
first spoken reply out).

### API key

The LLM tool-call path (any plain-text command, not a direct `{"tool": ...}` call) needs an
Anthropic API key. Put it in a `.env` file at the **repo root** (not inside `/agent`):

```
ANTHROPIC_API_KEY=sk-ant-...
```

`agent/agent/__main__.py` calls `load_dotenv()` on startup, which searches the current
directory and walks up through parents until it finds a `.env` — so the repo-root file is
picked up automatically regardless of where the agent process's cwd is. `.env` is gitignored;
never commit it.

`ELEVENLABS_API_KEY=...` goes in the same file if you want the optional ElevenLabs voice
(see "Voice output" below). It's read from the environment only — never stored in
`voice_settings.json` or sent to the UI.

### Persona / behavior (`SYSTEM_PROMPT.md`)

`agent/SYSTEM_PROMPT.md` is a plain markdown file that shapes how Claude behaves — it's
loaded once at startup (`agent_loop.py`) and passed as the `system` parameter on every
conversational LLM call. It's genuinely meant to be hand-edited: change the persona, the
tone, the behavioral rules, whatever — it's not buried in Python. Currently covers:
- **Identity**: the assistant is "Leutheria," with real tool access, not just advice.
- **Tone**: terse, conversational, spoken-style — since every reply gets read aloud via
  TTS. Explicitly told not to use markdown (`**bold**`, bullet points, backticks, etc.),
  since that gets synthesized as literal punctuation and sounds broken out loud.
- **Behavioral rules**: prefer an existing skill over re-deriving the same steps, explain
  destructive actions briefly before the confirmation prompt gates them, ask one short
  clarifying question rather than guess on genuinely ambiguous requests, prefer official
  APIs over scraping when building new capabilities.

Deliberately **not** applied to every LLM call — only `agent_loop.py`'s conversational
loop gets it. `skill_learning.py`'s calls (which must return strict JSON, nothing else)
pass no `system` at all, so persona/tone instructions never bleed into and corrupt a
structured-output call. See `LLMBackend.generate()`'s docstring in `agent/core/llm.py`
for this reasoning where it's implemented.

Changes to this file take effect on the next agent restart (it's read once at import
time, not hot-reloaded).

## Running the full app (Electron + Python together)

```bash
cd app
npm start
```

This spawns the Python agent automatically, connects to it, and opens the app window.
The sidebar's status dot goes green once the bridge is connected.

> **Known quirk in some sandboxed/CI shells:** if `ELECTRON_RUN_AS_NODE=1` is set in
> your environment, Electron silently runs as plain Node and the app APIs
> (`app`, `BrowserWindow`, `nativeImage`, etc.) will be `undefined`. If you hit that,
> run instead:
> ```bash
> env -u ELECTRON_RUN_AS_NODE npm start
> ```
> A normal terminal usually won't have this set at all.

### The interface

Six views in the left sidebar:

| view | what it's for |
|---|---|
| **Assistant** | The conversation. Type or talk; confirmations and skill proposals appear inline as cards. |
| **Library** | Every tool and skill in one master/detail list. Select one to read its description and parameters, see how often it's actually been used, turn it off, make it always ask first, or (for a learned skill) inspect its steps and delete it. |
| **Activity** | How the skill set has grown: every skill proposed, saved, or turned down, plus a most-used breakdown. |
| **Permissions** | Standing approvals (the `remember: session/always` ones) with a revoke button, and every capability whose settings differ from its default, with a reset. |
| **Voice** | Settings for the always-listening session: wake word on/off, which wake words, sensitivity, the end-of-utterance pause, the silence timeout, dismiss phrases, and the microphone. See below. |
| **Logs** | The raw bridge stream, out of the way of the conversation. Copy or clear it. |

Light and dark themes both ship; toggle at the bottom of the sidebar (the choice sticks).

**Library controls, and what they actually do:**
- **Available to Leutheria** — off means the capability isn't included in the tool schemas
  sent to Claude at all, so it can't be used *or planned around*. `dispatcher.dispatch()`
  also refuses it independently, which covers a stale plan or a learned skill with that
  tool baked into one of its steps.
- **Ask before running** — overrides the built-in policy in either direction. Turn it on
  for a safe tool you'd rather supervise; turn it off for `run_command` if you really want
  to (it's the one genuinely unbounded tool, so that's your call to make deliberately).
  Skills default to off because each of their *steps* dispatches back through the same
  gate and confirms on its own.
- **Delete** — learned skills only. A hand-written skill is code, not data, so it can be
  disabled from here but not deleted; the UI hides the button rather than failing after
  the fact.

Both settings live in `agent/preferences.json` and survive restarts. Deleting a learned
skill also clears its overrides, so a later skill that happens to reuse the name doesn't
silently inherit the dead one's settings.

**The Voice view.** Edits collect in a draft; **Save changes** sends one
`set_voice_settings` carrying only the keys you changed, and **Revert** drops the draft.
The form only checks that values are well-formed (a number, at least one wake word); the
agent owns the ranges, and since it refuses the whole update on the first bad key, its
message is shown against that field and nothing is saved. Changes apply live: turning
listening off closes the mic and the header pill goes to Off.
- **Wake words** lists openWakeWord's pretrained models (`list_wake_models`); ones not yet
  on disk are marked and download the first time they're used. `timer` and `weather` are
  intent models, not wake words. A custom trained `.onnx` can be added by path; the agent
  checks the file exists before it's accepted.
- **Microphone** lists input devices (`list_input_devices`, which only enumerates, never
  opens a stream). A new pick is stored by name, which survives devices being renumbered.
  PortAudio snapshots the device list when the agent starts; the reload button makes it
  re-read the list (a mic plugged in since launch), briefly closing and reopening the mic
  outside a session, and is refused during one.
- **Silence timeout** has a **Never** box: the session then ends only on a dismiss phrase
  or Stop listening.
- **Voice output** picks the engine replies are spoken with (Piper or ElevenLabs) and an
  optional voice id; switching engines clears the voice id, since one engine's id means
  nothing to the other. With ElevenLabs selected, it warns when the agent reports no
  `ELEVENLABS_API_KEY` (`tts_available` in `get_voice_settings`), since replies would
  still come out in Piper's voice.

### Testing the agentic loop

The Assistant view's **input at the bottom** — type a plain-English command
and press Enter to exercise the real LLM agentic loop (item #4): the text goes to Claude
along with the tool schemas, and Claude can call a tool, see the result, and call
another tool before giving a final answer — it's a loop, not a single shot. Each tool
call in the chain shows as its own row above the reply, with a green dot for success and
a red one for a failure or decline.
Try things like:
- `open the Calculator app` → should actually open Calculator
- `what is 12 times 7?` → should just get a text reply, no tool calls
- `open duckduckgo` → watch it call `list_files` to check what's installed first, *then*
  `open_app` — this is the loop actually working, not a single blind guess
- `run the shell command: echo hello` → a real confirmation prompt appears in the chat
  with Approve/Decline buttons (item #5). The agent loop actually **pauses** mid-flight
  waiting for your click — nothing runs until you approve, and declining feeds that back
  to Claude as a failed tool call so it can react accordingly
- `delete everything on my desktop` → Claude tends not to even attempt `run_command`
  blindly for a vague destructive request like this — expect it to list the folder and
  ask you to clarify what's actually safe to delete first, independent of the
  confirmation gate above

### Voice input (push-to-talk)

Click the mic button next to the input to start recording, click it again (now a stop
square, pulsing red) to stop. macOS will prompt for microphone access the first time — grant it to
Electron. On stop, the clip is sent to the agent, transcribed locally with Whisper, and the
transcript appears as your own message (labelled `you · voice`) before the normal agentic loop
runs on it exactly as if you'd typed it. It's click-to-start/click-to-stop, which is
simpler to get right than global hotkey hold-detection. For hands-free use there's now the
always-listening voice session below; push-to-talk stays as the no-wake-word path, and both
feed the same transcript handling in `server.py`.

### Voice output (TTS)

Every final text reply is also spoken out loud automatically — no toggle needed. The
agent synthesizes it locally with Piper right after sending the text response and plays
it through the Mac's speakers **itself** — the renderer never touches the audio. This
applies to typed, spoken, and tool-triggered replies alike, since it's wired in at the
point where the final response is sent, not per input method. If synthesis or playback
fails for any reason, it's logged (`tts_error` in `agent/logs/events.jsonl`) but never
blocks or delays the text reply itself — TTS is additive, not a dependency of the core
loop.

**Two voice engines** behind one `TTSBackend` interface (`agent/core/tts_backends.py`,
mirroring `llm.py`'s `LLMBackend`): each returns int16 samples plus a sample rate, so
playback, the `speaking` messages and the half-duplex mic muting don't know which one ran.

- **Piper** (the fallback): local, free, offline. Voice `en_US-amy-medium` unless `tts_voice`
  names another Piper voice (e.g. `en_GB-alan-medium`), which downloads on first use.
- **ElevenLabs** (default): noticeably more natural, but it's a cloud call — the reply text
  leaves the Mac, it needs internet, and it's billed per character on your own ElevenLabs
  plan. It uses the `eleven_flash_v2_5` model (lowest latency, half the per-character price
  of the multilingual models; override with `ELEVENLABS_MODEL_ID`) and adds a network round
  trip before speech starts (the whole reply is synthesized before playback, as with Piper).
  Needs `ELEVENLABS_API_KEY` in the repo-root `.env`; `tts_voice` is an ElevenLabs voice ID,
  default `JBFqnCBsd6RMkjVDRZzb` ("George", a premade voice). **Without a key, every reply
  is spoken by Piper** (and logs a `tts_error` saying the key is missing) — pick Piper in
  the Voice view to stay fully local and silence that.

Pick one in the Voice view's **Voice output** card, or with `set_voice_settings`
(`tts_backend`, `tts_voice`). The settings are read per utterance, so the next reply uses
them — no restart. **Fallback:** if the selected backend fails — no key, network error,
HTTP error (a bad key is a 401), a timeout (5 s connect, 20 s read) — that utterance is
spoken by Piper's default voice instead and a `tts_error` is logged with the `backend`
that failed and why. A reply is never silent because of the premium voice. The same goes
for a Piper voice name that doesn't exist. (Piper's GPL licensing question is in
`plans/NOTES.md` #1.)

`agent/scripts/tts_check.py` checks all of this without a real key, against a fake
ElevenLabs server on localhost: the request shape, PCM decoding, the `speak()` path, each
fallback, and settings validation. Nothing is played aloud.

Why the sidecar plays it rather than the renderer (which it used to, via a base64
`speech` message): the always-listening mic below must not hear Leutheria's own voice,
and only the process driving the speaker knows exactly when playback starts and stops.
It also means replies are heard with every window hidden. One voice at a time — two
replies finishing together queue rather than talk over each other.

### Always-listening voice session

Say **"hey Jarvis"** (a pretrained openWakeWord model — a custom "hey Leutheria" needs a
trained model, and drops in via the `wake_models` setting without code changes), then just
talk. The session stays open across as many requests as you like: each is transcribed,
run through the same agent loop as a typed command, and the reply spoken; then it's
listening again. It ends when you say something that's essentially a goodbye — "thanks",
"thank you", "that's all", "that's it", "we're done", "goodbye" — or after
`silence_timeout_s` (default 3 minutes) with nothing said, or when you end it from the UI.
A session can also be started without the wake word (`voice_session` `start`, below), and
that works even with voice disabled — the mic opens for that one session and closes again.

How it works, all in `agent/core/voice_session.py`:

- The sidecar captures the mic itself (sounddevice, 16 kHz mono, 80 ms frames). Idle, each
  frame goes to openWakeWord (ONNX — its default tflite backend has no Python 3.12 macOS
  wheel). In a session, each frame goes to the Silero VAD that ships with openWakeWord
  (picked over webrtcvad because it tells speech from steady noise — fans, keyboards,
  music — much better, at no extra dependency).
- An utterance ends after `endpoint_silence_ms` (800 ms) of trailing silence, and goes to
  Whisper in an executor.
- **Half-duplex:** while Leutheria is talking, plus a 300 ms echo tail, captured audio is
  thrown away — no wake word, no VAD. There's no barge-in yet. While a command is running
  (and not waiting on a confirmation) audio is discarded too: one command in flight.
- **Guards against Whisper's hallucinations** — it famously "hears" "Thank you." in
  silence, which would dismiss the session. An utterance only reaches Whisper if VAD heard
  at least 300 ms of actual speech; segments with `no_speech_prob` over 0.6 are dropped;
  a transcript that's only "hm"/"uh"/"um" is dropped; and a dismissal only counts if the
  utterance is five words or fewer *and* nothing but the phrase plus filler ("okay, that's
  all for now") — "thanks, what's the weather" is a request, not a goodbye.
- **Confirmations work hands-free.** A destructive step's prompt is spoken, then the mic
  opens (`awaiting_confirmation`) for a yes/no, answered through the same
  `interpret_yes_no()` path as push-to-talk. Unlike push-to-talk, anything that *isn't* a
  clear yes/no is shown in the chat but not run as a new command — a second command
  starting while the first is paused on a question would break "one command in flight".
  The card stays open for another try or a click.
- Voice commands run on the most recently connected UI (the "primary"), so confirmation
  cards and skill proposals land there. With no UI connected they still run — and a
  destructive step fails closed, since there's nothing to confirm through.
- No mic, a failed model download, a device unplugged mid-session: logged as
  `voice_error`, voice goes to `off`, and everything else carries on. Any settings change
  retries.

macOS shows its orange mic indicator the whole time voice is enabled — the mic really is
open. The first time, macOS asks for microphone permission for whatever launched the
agent (Electron when run via `npm start`, your terminal when standalone).

**Settings** (`agent/voice_settings.json`, gitignored; read and written over the protocol
below, applied live):

| setting | default | |
|---|---|---|
| `enabled` | `true` | wake word listening on/off |
| `wake_models` | `["hey_jarvis"]` | pretrained openWakeWord names, or paths to a trained `.onnx` |
| `wake_threshold` | `0.5` | 0–1 |
| `silence_timeout_s` | `180` | a session with nothing said for this long ends; `null` = never (only a dismiss phrase or Stop listening ends it) |
| `endpoint_silence_ms` | `800` | trailing silence that ends an utterance |
| `dismiss_phrases` | thanks, thank you, that's all, that's it, we're done, goodbye | |
| `input_device` | `null` | sounddevice index or name; `null` is the system default |
| `tts_backend` | `"elevenlabs"` | `"piper"` or `"elevenlabs"` — the engine replies are spoken with (see "Voice output") |
| `tts_voice` | `null` | the selected engine's voice id (letters, digits, `-`, `_`); `null` is its default |

**Testing it without a microphone.** Two scripts in `agent/scripts/`, neither of which
opens the mic:

```bash
cd agent
./.venv/bin/python scripts/voice_sim.py            # every scenario, ~2 min
./.venv/bin/python scripts/voice_sim.py confirm    # one; --list names them
./.venv/bin/python scripts/voice_server_check.py   # the real server on port 8781
```

`voice_sim.py` stitches Piper utterances ("Hey Jarvis." … "What time is it?" …
"Thanks.") with silence and feeds them through the session's injectable frame source at
real speed, wired to the real server hooks, and checks the exact `voice_state` sequence
per scenario: dismissal, a long sentence containing "thanks" (not a dismissal), silence
timeout, noise and blips (never a command), a spoken confirmation answered by voice, and a
manual start/end. The agent loop is a canned responder and playback is a silent sleep, so
it needs no API key and makes no sound (`--real-llm`, `--play` to change that).
`voice_server_check.py` runs `python -m agent` with `LEUTHERIA_PORT=8781` and
`LEUTHERIA_VOICE_SOURCE=<wav>` (a WAV in place of the mic — also usable by hand), then
exercises the control messages, a live session through the real agent loop (needs the
API key; the reply *is* played aloud), push-to-talk, and settings surviving a restart.
It backs up and restores `voice_settings.json`.

Note: `piper-tts` is GPL-3.0-or-later licensed. Fine for local development; worth
revisiting the licensing implications before any public release/distribution.

### Voice sessions: the overlay, menu bar and shortcut

A voice session is the hands-free mode: once it starts, Leutheria keeps listening,
answering and listening again until you say something like "thanks" or "that's all", or
it hears nothing for a long while. The mic, wake word and playback all live in the
Python sidecar; the Electron side only shows the session and starts or stops it.

**Starting and stopping.** A session starts on the wake word, or manually from any of:
- **⌥Space** (Alt+Space), a global shortcut that toggles a session from any app;
- the **menu bar icon** → *Start listening* / *Stop listening* (only the one that makes
  sense right now is enabled; the menu also has *Show Leutheria* and *Quit*);
- the **voice indicator** at the right of the main window's header, which also shows the
  current state: Off, Idle, Listening, Thinking, Speaking, or Needs approval.

Closing the main window no longer stops the agent: a session keeps running, and the
menu bar icon brings the window back.

**The overlay** (`app/renderer/overlay/`) is a small floating "island" at the top-center
of whichever display the cursor is on. It fades in when a session starts and out when it
ends, and shows the state (listening, hearing, transcribing, thinking, speaking, waiting
for approval), what you said, and a short form of the reply. It is built never to get in
the way: it floats above full-screen apps and every Space, appears without taking focus,
and is **click-through**; clicks land on whatever is underneath. The one exception is a
confirmation: while a compact Approve / Decline card is showing, the overlay accepts
clicks, and it goes click-through again as soon as the card is answered (by click, by
voice, or from the main window's full card, which still offers the "remember" options).
Answering in either place settles the other.

The overlay follows the main window's light/dark choice (and the macOS appearance if none
was made).

**Visual assets** live in `app/renderer/overlay/assets/`. `manifest.json` maps each state
to a built-in CSS placeholder, an image (png/gif/svg/webp) or a video (transparent webm);
swapping in real artwork is a file copy plus a manifest edit, no code change. The mic level
is exposed as the CSS variable `--level` (0 to 1) for reactive visuals. That folder's
`README.md` is written for the asset designer: which states exist and when, formats and
sizes, and how to preview.

**Voice settings** (wake model, threshold, timeouts, dismiss phrases, input device) are
edited in the **Voice** view (see The interface above), which uses
`window.leutheria.getVoiceSettings()` / `setVoiceSettings(partial)` / `listInputDevices()` /
`listWakeModels(paths)`: the `get_voice_settings` / `set_voice_settings` /
`list_input_devices` / `list_wake_models` control messages.

### Testing the Electron side without Python

`app/scripts/mock-agent.js` is a scripted stand-in for the agent on port 8799 (never
8765). It plays a complete voice session: wake, listening with mic levels, hearing,
transcribing, a transcript, thinking, a confirmation (waits for a click, then "answers by
voice" if none comes), the spoken reply, and a dismissal. It also answers the control
messages, and a typed command containing "confirm" runs a push-to-talk confirmation round
trip. Two environment variables point the app at it:

| variable | effect |
|---|---|
| `LEUTHERIA_AGENT_URL` | Connect to this WebSocket URL instead of `ws://127.0.0.1:8765` |
| `LEUTHERIA_NO_SPAWN=1` | Don't start (or pkill) the Python agent; keep retrying the connection instead |
| `LEUTHERIA_CAPTURE_DIR` | Debug only: write a PNG of the overlay and main window on every voice-state change, into this folder (which also gets its own `userdata/`, so a debug run never shares storage with a real one) |
| `LEUTHERIA_CAPTURE_THEME` | With the above: force the overlay to `light` or `dark` |

```bash
cd app
node scripts/mock-agent.js &
env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
```

`MOCK_LOOP=1` replays the session after each one ends; `MOCK_AUTOSTART=0` waits for
⌥Space or the menu instead; `MOCK_CONFIRM_TIMEOUT_MS` sets how long it waits for a click.

## Running the Python agent standalone (no Electron)

Useful for testing tools directly without going through Electron.

```bash
cd agent
./.venv/bin/python -m agent
```

This starts the WebSocket server at `ws://127.0.0.1:8765` and prints `AGENT_READY`
once it's up (`LEUTHERIA_PORT=8781` picks another port, to run beside the app). With voice
enabled it also opens the microphone; `LEUTHERIA_VOICE_SOURCE=/path/to.wav` feeds that WAV
in instead (at real speed, then silence), so the voice session can be driven without one. Leave it running in one terminal, then in another terminal use a small
Python client to send it commands:

```bash
cd agent
./.venv/bin/python -c "
import asyncio, json, websockets

async def main():
    async with websockets.connect('ws://127.0.0.1:8765') as ws:
        await ws.send(json.dumps({
            'type': 'command',
            'tool': 'list_files',
            'args': {'path': '~/Desktop'},
        }))
        print(await ws.recv())

asyncio.run(main())
"
```

### Available tools

| tool | args | safety | notes |
|---|---|---|---|
| `open_app` | `{"name": "Calculator"}` | safe | shells out to `open -a` |
| `create_folder` | `{"path": "~/Desktop/test"}` | safe | `mkdir -p` equivalent |
| `list_files` | `{"path": "~/Desktop"}` | safe | lists directory contents |
| `run_command` | `{"cmd": "echo hi"}` | **destructive** | pauses for a live user confirmation before it runs — see "Confirmation flow" below |
| `get_clipboard` | `{}` | safe | reads clipboard text |
| `set_clipboard` | `{"text": "..."}` | safe | copies text to clipboard |
| `open_url` | `{"url": "https://..."}` | safe | opens in default browser |
| `show_notification` | `{"title": "...", "message": "..."}` | safe | macOS notification banner |
| `take_screenshot` | `{"save_path": "..."}` (optional) | safe | defaults to a timestamped file on the Desktop |
| `read_file` | `{"path": "..."}` | safe | text content, capped at 20,000 chars |
| `append_text` | `{"path": "...", "text": "..."}` | safe | append-only — can't overwrite, so no confirmation needed |
| `trash_file` | `{"path": "..."}` | safe | moves to the system Trash — recoverable, unlike `rm` |
| `move_file` | `{"source": "...", "destination": "..."}` | safe | refuses rather than overwriting an existing destination |
| `copy_file` | `{"source": "...", "destination": "..."}` | safe | same overwrite guard as `move_file`; handles files and folders |
| `list_running_apps` | `{}` | safe | visible, foreground application names |
| `get_battery_status` | `{}` | safe | percent + charging state, if this Mac has a battery |
| `get_disk_space` | `{}` | safe | total/used/free GB on the main disk |
| `get_current_datetime` | `{}` | safe | current date and time |
| `focus_app` | `{"name": "TextEdit"}` | **scoped** | brings an app to the front and makes it the working context |
| `describe_ui` | `{"app": "TextEdit"}` (optional) | safe | the app's Accessibility tree; password fields redacted |
| `type_text` | `{"text": "..."}` | **scoped** | types into whatever has keyboard focus |
| `press_keys` | `{"keys": "cmd+n"}` | **scoped** | one keyboard shortcut to the frontmost app |

Example calls for each (swap the `tool`/`args` fields in the snippet above):

```python
{"type": "command", "tool": "open_app", "args": {"name": "Calculator"}}
{"type": "command", "tool": "create_folder", "args": {"path": "~/Desktop/leutheria-test"}}
{"type": "command", "tool": "list_files", "args": {"path": "~/Desktop"}}
{"type": "command", "tool": "run_command", "args": {"cmd": "echo hi"}}   # pauses for confirmation
```

Notice how many of these are "safe" despite touching the filesystem or running something — that's deliberate, not an oversight. Each one is designed so it structurally *can't* destroy data: `trash_file` uses the recoverable system Trash instead of a real delete, `move_file`/`copy_file` refuse outright rather than silently overwriting an existing destination, and `append_text` can only add content, never truncate or replace it. `run_command` is the one tool that's genuinely unbounded (arbitrary shell), which is exactly why it's the only one gated behind confirmation.

### Skills

A skill is a hand-written, multi-step procedure exposed to the LLM as if it were a single
tool — the point is to collapse what would otherwise take several separate LLM reasoning
turns (and round-trips) into one. `agent/skills/registry.py`'s `SKILLS` dict has the same
shape as `tools/registry.py`'s `TOOLS`, and `dispatcher.dispatch()` checks it first: if the
name matches a skill, it calls that skill's `run(args, websocket, pending)` directly
instead of doing the usual single-function tool call.

A skill's `run()` is free to call `dispatch()` itself for its own sub-steps — which means
a skill isn't a way to bypass safety tiering: if it calls a destructive tool like
`run_command` internally, that sub-step still pauses for its own confirmation prompt, same
as if the LLM had called it directly. `setup_project` (the first one, in
`agent/skills/setup_project.py`) demonstrates this: it runs `create_folder`
(safe, no prompt) then two `run_command` calls (`git init`, then opening the folder in
VS Code), each requiring its own approval.

Try it: `create a project called sample, initialize a repo and open in vs code` — watch
the trace show a single `setup_project` tool call (not three separate ones), with two
confirmation prompts along the way for the `git init` and VS Code steps. This is also a
good way to see the value directly: without the skill, the same request costs several
back-and-forth LLM turns as the model composes `create_folder` → `run_command` →
`run_command` on its own reasoning; with the skill, it costs one.

**A caveat worth remembering**: the very first version of `setup_project` had a real bug —
it built its shell commands with a `~`-prefixed path that `/bin/sh`'s `cd` doesn't expand
inside quotes, so `git init` silently failed. Claude actually noticed the failure and
worked around it live by issuing its own follow-up `run_command` calls with an absolute
path — which shows the agentic loop's fallback reasoning works, but is not a substitute
for the skill itself being correct. Fixed by expanding `~` via `Path(...).expanduser()`
before building any shell string. The fix cut the request from 5 LLM turns down to 2
(one to call the skill, one to summarize) — check `agent/logs/events.jsonl`'s `llm_turn`
entries to see this kind of thing for yourself on any request.

Two more hand-written skills exist, added deliberately rather than waiting for
self-learning to invent them: `quick_note` (append a timestamped note, confirm with a
notification -- try `remember that I need to renew my passport`) and `backup_folder`
(copy something to a timestamped backup next to itself -- try `back up my project folder
before I change anything`). Both compose only safe tools, so unlike `setup_project`
they run in a single shot with no confirmation prompts along the way.

### Automatic skill learning

`setup_project` above is hand-written. The agent can also **propose its own skills** —
this is scoped to skills only for now, not brand-new tools (see `plans/NOTES.md` for why
that distinction matters: a skill can only ever recombine tools a human already reviewed
and safety-tagged, whereas a wholly new tool would be unreviewed code with nobody having
assigned it a safety tier — a meaningfully bigger trust step, deliberately deferred).

**Proposes on the very first success, not just on repeats** — but a first-time proposal
generalizes from a single example, so anything genuinely ambiguous gets asked as a
clarifying question instead of silently guessed:

1. Any request that finishes with 2+ tool calls, **where every one of them succeeded**,
   and isn't already covered by an existing skill, is a candidate. The clean-success
   requirement matters: without it a run where the app never launched (so `focus_app` and
   `describe_ui` both failed) still produced a `create_titled_note` proposal built from
   the failing sequence — approving it would have saved a skill that reproduces the
   failure on demand. `agent/core/skill_learning.py` checks `agent/logs/events.jsonl`
   for a *past* run with the same ordered tool-name sequence — this check runs **before**
   the current request logs its own entry (a real bug caught during testing, see
   `plans/BUGS.md` #6: checking after would trivially match the request against itself).
2. **If a past match exists** (a genuine repeat): Claude diffs the two concrete runs —
   confident, since two data points show what actually varied vs. stayed constant.
3. **If this is the first time** the sequence has been seen: Claude generalizes from the
   one example it has. Anything it's confident about (like a name you explicitly said,
   e.g. "foo1") becomes a real parameter immediately. Anything it can't be sure of from
   one example (like a location that only appeared once, e.g. "~/Desktop") comes back as
   an `uncertain_params` entry with a plain-language question instead of a guess.
4. Either way, the result is a skill *definition* — just JSON, never new Python. A
   `skill_proposed` message goes to the chat UI: any clarifying questions render as
   "It varies" / `Always "<value>"` toggles (defaulting to "it varies" if never touched —
   fails toward flexibility, not toward silently hardcoding something that might change),
   followed by Save/Decline for the whole proposal. Entirely non-blocking throughout —
   never delays or affects the response you already got, same principle as TTS.
5. On Save, `agent/core/skill_learning.py`'s `finalize_definition()` applies your answers
   (baking any "always this value" choice into the steps as a literal and dropping it from
   the schema; leaving "it varies" answers as real parameters), then the result is written
   to `agent/skills/generated/<name>.json` and registered immediately — no restart needed.
   It runs through the one shared `template_skill.run_template()` interpreter, which just
   substitutes parameter values and calls `dispatch()` per step — a generated skill is
   structurally incapable of doing anything a hand-written skill couldn't, and any
   destructive step inside it still pauses for its own confirmation.

Try it: ask `create a folder called foo1 on my desktop and list what's in it` — you should
get a proposal immediately, with a clarifying question about whether new folders always
go on the Desktop. Answer it either way and save, then try a *differently-phrased* new
request (`make me a folder named testxyz and show its contents`) — it should match
semantically and run as a single tool call using the generated skill.

Declining is remembered — a `skill_declined` event records the tool-sequence signature
(not the generated name, which can vary between attempts), and that signature is checked
before ever proposing again, so a declined pattern doesn't keep coming back.

#### Generated skill format

A generated skill is written to be safe to hand to someone else, even though nothing is
shared yet: the rules below hold for a file nobody on this machine wrote, so they reject
rather than repair. They live in `agent/skills/validate.py` and run at three points: when
a proposal is generated (an invalid one is logged as `skill_proposal_rejected` and never
shown — a Save button that can't work is worse than no proposal), when it's saved
(`register_skill()`; nothing is written unless the whole definition passes), and when
`generated/*.json` is loaded at startup (a bad file is logged as `skill_load_rejected` with
the reason and skipped; it never stops startup or the other skills).

```json
{
  "name": "make_and_list",
  "description": "Create a folder and list what's inside it.",
  "input_schema": {"type": "object",
                   "properties": {"folder": {"type": "string", "description": "..."}},
                   "required": ["folder"]},
  "steps": [{"tool": "create_folder", "args": {"path": "~/Desktop/{folder}"}},
            {"tool": "list_files",    "args": {"path": "~/Desktop/{folder}"}}],
  "source_signature": ["create_folder", "list_files"],
  "id": "3f0c…",                 "version": 1,        "source": "learned",
  "content_hash": "9a1e…",       "requires": ["create_folder", "list_files"],
  "created_at": 1790798842.2
}
```

- **Steps call tools only, never skills** — hand-written or generated. Every generated
  skill stays a flat list you can read top to bottom: no recursion, and nothing hidden
  behind another skill's name. `run_template()` re-checks this at run time. A trace that
  used a skill isn't proposed at all, since it could never validate.
- **Name** matches `^[a-z][a-z0-9_]{0,63}$`, isn't a Windows device name (`con`, `nul`,
  …), and isn't a tool's or hand-written skill's name — `dispatch()` resolves skills
  first, so a generated skill named after a tool would shadow it. The file path is built
  only from the validated name and must resolve directly inside `generated/`; on load the
  file name must equal the name inside it, and symlinks are refused. A proposal whose
  name is taken gets `_2`, `_3`… before it's shown.
- **Steps**: 1–50 of exactly `{tool, args}`. `args` is plain JSON (no NaN/Infinity,
  string keys, bounded depth/length), uses only argument names the tool declares, and
  supplies every argument the tool requires.
- **input_schema**: `type: object` with `properties` / `required` only; each parameter is
  `string` / `number` / `integer` / `boolean` with optional `description`, `enum`,
  `default`. At most 20.
- **Placeholders**: every `{param}` must be declared, and every declared parameter must be
  used by some step — an **error**, not a warning: a parameter the skill ignores is an
  interface that lies to the model. `finalize_definition()` prunes unused *optional*
  ones, so what fails is a genuinely broken generalization.
- **description**: non-empty, at most 1000 characters. Unknown top-level keys are refused.

**Placeholder grammar** (`template_skill.py`): `{param}` is replaced, `{{` / `}}` are
literal braces, and anything else with braces — `{}`, `{print $1}`, a lone `{` — is
literal. Substitution is one pass, so a value containing `{other}` is inserted as-is and
never re-expanded; a string that is exactly one placeholder keeps the value's JSON type.
This replaced `str.format`, which gave up on the whole string at the first brace it
couldn't resolve (`plans/BUGS.md` #24). A skill run missing a parameter with no `default`
fails before any step runs.

**Metadata**, stamped by `register_skill()`:

| Field | Meaning |
|---|---|
| `id` | uuid4, fixed for the skill's life. Duplicate ids on disk are refused. |
| `content_hash` | sha256 of canonical JSON (sorted keys, no whitespace, UTF-8, domain-prefixed `leutheria-skill-content-v1`) over the **steps** and the **behavioural part of input_schema** — parameter names, types, enums, defaults, sorted `required`. Name, description and parameter descriptions are left out on purpose: two users who learned the same procedure and worded it differently get the same hash, which is what a shared database would dedupe on. So it identifies *behaviour*; it is not an integrity check of the file's prose. |
| `version` | Starts at 1. A learned skill whose content was hand-edited on disk gets its hash recomputed and its version bumped on the next load (`skill_migrated`). |
| `source` | `"learned"`, or (reserved for later) `"imported:<sha256>"`. An imported skill whose content no longer matches its hash is rejected, not re-hashed. |
| `requires` | Sorted tool names the steps use — what a skill needs enabled to work. |
| `created_at` | Unix seconds. |

A file from before metadata existed is migrated on load: the metadata is computed and
written back (`created_at` from the file's mtime), everything else unchanged. Inventory
exposes `id` / `version` / `source` / `content_hash` / `requires` on each skill row (null for
hand-written skills); the Library doesn't render them yet.

`./.venv/bin/python scripts/skill_format_check.py` exercises all of this against temp
directories.

### Confirmation flow

Any tool tagged `"destructive"` in `agent/tools/registry.py` (currently just `run_command`)
doesn't execute immediately. Instead, `dispatcher.dispatch()` sends a
`confirmation_required` message back over the same connection and **suspends** until a
matching `confirm` message arrives (or `CONFIRMATION_TIMEOUT` — 120s — elapses, which
counts as a decline):

```python
# 1. you send:
{"type": "command", "tool": "run_command", "args": {"cmd": "echo hi"}}

# 2. agent replies immediately with:
{"type": "confirmation_required", "id": "<uuid>", "tool": "run_command", "args": {"cmd": "echo hi"}}

# 3. you send back:
{"type": "confirm", "id": "<uuid>", "approved": true, "remember": null}   # or "session" / "always"

# 4. only now does the agent send the real result:
{"type": "tool_result", "tool": "run_command", "result": {"ok": true, "stdout": "hi", "stderr": ""}}
```

This works identically whether the destructive tool was called directly or picked mid-loop
by Claude — in the LLM path, a decline gets fed back to Claude as a failed tool result, so
it can explain what happened instead of the connection just hanging. In the Electron app,
this whole exchange is automatic: the confirmation shows up in the transcript as an
"Approval needed" card with four buttons — Approve / Approve for this session / Always
approve / Decline (`app/renderer/js/chat.js`) — and clicking one sends the `confirm`
message for you. Once answered the buttons are replaced by a line saying what was
decided, including when it was answered by voice instead of clicked.

**Trust (`agent/core/trust.py`)**: `remember: "session"` or `"always"` tells
`dispatcher.dispatch()` to skip the confirmation prompt entirely next time — but only for
the *exact same* `(tool, args)` pair, checked via `trust.is_trusted()` before a
confirmation is even requested. `"session"` is an in-memory set that resets when the
agent restarts; `"always"` additionally persists to `agent/trusted_commands.json`
(gitignored — local state, not source) and is checked on every future startup. This is
deliberately narrow: trusting one specific `git init` in one specific directory can never
blanket-trust a *different* `rm` command elsewhere just because both happen to be
`run_command` calls — only a genuinely identical repeated invocation skips the prompt.

### Controlling other apps (the `scoped` tier)

`open_app` launches something; the scoped tools actually *drive* it. Together they cover
the case a shell command can't reach — "open my notes app," then, sporadically over the
next ten minutes, "add a line about X" without ever naming the app again:

```
focus_app    bring an app forward and make it the session's working context
describe_ui  read its on-screen structure -- fields, buttons, what has focus, what they contain
type_text    type at the cursor
press_keys   a keyboard shortcut (cmd+n, cmd+s, escape...)
```

This is the first capability in the codebase that **isn't safe by construction**, and it
gets a different shape of gate as a result. A shell tool's blast radius is bounded by its
arguments; `type_text` goes wherever the cursor is. So:

- **Approval is per app, not per call, and time-boxed.** Nobody clicks Approve two hundred
  times for two hundred keystrokes. The first time Leutheria reaches for an app you get one
  prompt; approving grants that app for 15 minutes, and the typing that follows doesn't ask
  again. Grants live in memory only — they die with the agent process, unlike a
  `remember: always` trust — and the Permissions view lists every live one with a Revoke
  button.
- **Some apps can never be driven or read at all**, grant or no grant: Terminal, iTerm,
  Keychain Access, System Settings, password managers (`DENIED_APPS` in
  `agent/core/ui_access.py`). Terminal is on that list specifically because typing into a
  shell would turn `type_text` into `run_command` with no confirmation — the denylist is
  what stops the GUI tier from being a hole straight through the safety model.
- **`describe_ui` stays unprompted**, because looking before acting is what makes the loop
  work — the same reason `list_files` doesn't ask. It's safe by construction rather than by
  policy: `AXSecureTextField` values are never returned, and the denylist applies to reads
  too.
- **Focus is re-checked before every chunk of typing**, and typing aborts mid-stream if it
  moves. The whole point is a session where you keep using your computer, so focus changing
  mid-sentence isn't an edge case. Without this, approving a note app and then clicking to
  Slack would send the rest of the sentence into a chat box. A partial write is reported as
  a failure with the character count, never rounded up to success.
- `type_text` also reports **where** it typed (window title and focused control), and warns
  when that's a dialog or a non-text control. Found in testing: opening TextEdit put its
  *Open* panel in front, and a perfectly "successful" type went into the file picker.

**Requires Accessibility permission**, which macOS grants to the *responsible* process —
Electron under `npm start`, or your terminal if you ran the agent standalone. Untrusted AX
calls don't error, they return empty trees, so `ui_access.ensure_trusted()` checks
explicitly and fails loudly. Grant it in System Settings > Privacy & Security >
Accessibility.

### Conversation continuity

Every request used to be independent: `agent_loop.run()` built a fresh `messages` list, so
nothing could refer back. `agent/core/session.py` adds the two pieces of state a
multi-minute working session needs:

- **focus** — the app last successfully focused, injected into the system prompt ("You are
  currently working in TextEdit"), so a follow-up doesn't have to re-name it.
- **history** — a rolling window of plain user/assistant turns. Only text is carried; the
  `tool_use`/`tool_result` blocks stay local to their own request, since replaying a stale
  tool trace invites the model to treat an old result as still true.
- **actions** — which tools each recent request actually ran, and whether they
  succeeded, listed in the system prompt as past events. Names and arguments only, never
  results. Text-only history on its own made the model disown its own replies: shown
  "Ran it, the output was Hello" with no tool call behind it, it apologised for making
  that up and ran the command again.

All three expire after 10 minutes of silence, so a request made hours later doesn't silently
resolve "it" against something long forgotten.

Try it — three separate requests, the app named only once:

```
open textedit and write a heading that says Meeting notes
add a line under that saying budget review is on friday
actually add one more line about the hiring plan
```

Observed trace: `focus_app` → `describe_ui` (empty, so) → `press_keys cmd+n` →
`type_text`, then on the follow-ups `describe_ui` → `type_text` appending only the new
line, and on the third `press_keys cmd+down` to get to the end first.

### Voice-only confirmation

A confirmation can be answered by voice alone, with nothing to click — `_confirm()` in
`dispatcher.py` speaks the prompt as soon as it's sent, using `agent/core/tts.py`'s
shared `speak()` helper (the same one `server.py` uses for final replies). It prefers
Claude's own accompanying explanation for that turn (`SYSTEM_PROMPT.md` already asks it
to briefly explain a destructive action before taking it) and falls back to a generic
"I want to run `<tool>`. Should I go ahead?" if the model didn't say anything alongside
the tool call.

On the input side: when a confirmation is pending on a connection, the next transcribed
voice message is checked with `agent/core/voice_confirm.py`'s `interpret_yes_no()` — a
word-boundary regex match against common approve/decline phrasings ("yeah go ahead",
"nope", "cancel that", etc.) — *before* it's treated as a new command. A clear match
resolves the confirmation directly; anything ambiguous (neither/both matched, or clearly
an unrelated request like "open Safari") falls through to the normal command path
instead, leaving the confirmation open rather than guessing. This is what makes
"speak the whole way through, no clicking" actually work, not just the reply half of it.

Verified with real speech, not just injected text: a genuine `say`-generated "yeah go
ahead" clip, converted to webm and sent through the real audio pipeline, was correctly
transcribed by Whisper and resolved a pending confirmation — the original destructive
request then completed and spoke its own result, entirely hands-free end to end.

**Why the server needed restructuring for this:** the old handler processed one command,
waited for its full response, then read the next message — but if a command needs to
*pause* and wait for you to send an unrelated `confirm` message on the same connection,
that read loop can't be blocked on the paused command. `server.py`'s `handler()` now
dispatches each incoming command as a background `asyncio.create_task`, so it stays free
to read (and resolve) an incoming `confirm` while another command is mid-pause.

Sending a `text` field (no `tool`) drives the agentic loop instead — Claude sees the
tool schemas, can call one or more tools (seeing each result before deciding what's
next), and the response always comes back as `{"type": "response", ...}` with a `trace`
of every tool call made along the way (empty if it just answered directly):

```python
{"type": "command", "text": "open the Calculator app"}
# -> {"type": "response", "text": "Calculator is open...",
#     "trace": [{"tool": "open_app", "args": {"name": "Calculator"}, "result": {...}}]}

{"type": "command", "text": "what is 12 times 7?"}
# -> {"type": "response", "text": "12 × 7 = **84**", "trace": []}
```

This requires `ANTHROPIC_API_KEY` to be resolvable (see API key section above) — without
it, the agent will error when a text command comes in (the `{"tool": ...}` path above
doesn't need a key at all, since it skips the LLM entirely).

### Voice input protocol

Sending `{"type": "audio", "data": "<base64>", "format": "webm"}` transcribes the audio
locally with Whisper, sends back the transcript immediately, then feeds it through the
exact same agentic loop as a `text` command:

```python
{"type": "audio", "data": "<base64-encoded webm/wav/etc audio>", "format": "webm"}
# -> {"type": "transcript", "text": "open the calculator app", "origin": "push_to_talk"}   (sent first)
# -> {"type": "response", "text": "Calculator is now open...", "trace": [...]}  (final)
```

`transcript.origin` is `"push_to_talk"` here and `"voice_session"` for the live session,
whose replies also carry `"origin": "voice_session"` on the `response` — the renderer never
sent a request for those, so this is how the UI knows to show them.

`format` can be any container ffmpeg can decode (the Electron app sends `webm`, since
that's what the browser's `MediaRecorder` produces by default). No API key needed for
this part — Whisper runs fully locally.

### Voice output protocol

The agent plays every spoken reply (and every spoken confirmation prompt) itself, and
brackets the playback with `speaking` messages so the UI can animate while it talks:

```python
{"type": "response", "text": "12 × 7 = **84**", "trace": []}
{"type": "speaking", "state": "started", "text": "12 × 7 = **84**", "duration_ms": 2140, "backend": "piper"}
{"type": "speaking", "state": "ended"}
```

There's no request for this — it follows every non-empty text reply, for every input
method. `backend` is the engine that actually produced the audio — `"piper"` when an
ElevenLabs request failed or had no key and fell back to Piper, which runs fully locally. The old `{"type": "speech", "data":
<base64 WAV>}` message is **gone** — nothing sends audio over the socket any more.

### Voice session protocol

Stream events (to every connected UI; a newly connected one gets the current
`voice_state` straight away):

```python
{"type": "voice_state", "state": "listening", "session": true}
# state: off | idle | listening | hearing | transcribing | thinking | speaking | awaiting_confirmation
# -- sent on every transition
{"type": "session_started", "trigger": "wake_word", "wake_word": "hey_jarvis", "score": 0.93}
{"type": "session_started", "trigger": "manual", "wake_word": null, "score": null}
{"type": "session_ended", "reason": "dismissed"}   # | "timeout" | "manual" | "error"
{"type": "audio_level", "level": 0.42}   # 0..1, smoothed; once per 80 ms frame (12.5 Hz),
                                         # only in a session while listening/hearing/awaiting_confirmation
```

The transitions:

```
off ──(voice enabled)──▶ idle ──wake word / manual start──▶ listening
listening ──speech starts──▶ hearing ──endpoint (≈800 ms silence)──▶ transcribing
transcribing ──dismiss phrase──▶ idle (session_ended reason=dismissed)
transcribing ──empty/hallucination──▶ listening
transcribing ──command──▶ thinking ──reply──▶ speaking ──playback ends──▶ listening
thinking ──confirmation_required──▶ speaking(prompt) ──▶ awaiting_confirmation (mic live)
listening ──no speech for silence_timeout──▶ idle (session_ended reason=timeout)
any ──manual end──▶ idle (session_ended reason=manual)
```

A session ended while voice is disabled (a manual one) goes back to `off`, not `idle`.
Control messages, answered with `control_result` like the management ones below:

```python
{"type": "voice_session", "action": "start", "request_id": "1"}   # or "end"
# -> {"type": "control_result", "request_id": "1", "ok": true, "state": "listening", "session": true}

{"type": "get_voice_settings", "request_id": "2"}
# -> {..., "ok": true, "settings": {"enabled": true, "wake_models": ["hey_jarvis"], ...},
#          "tts_available": {"piper": true, "elevenlabs": false}}
# tts_available says whether each TTS engine is configured (for ElevenLabs: whether
# ELEVENLABS_API_KEY is set) -- a boolean, never the key.

{"type": "set_voice_settings", "settings": {"silence_timeout_s": 60}, "request_id": "3"}
# -> {..., "ok": true, "settings": {...the full merged settings...}}
# a partial object; merged, persisted, applied live. One bad or unknown key rejects the
# whole update: {"ok": false, "error": "wake_threshold must be between 0 and 1"}

{"type": "list_input_devices", "request_id": "4"}
# -> {..., "ok": true, "devices": [{"index": 3, "name": "MacBook Pro Microphone",
#                                   "channels": 1, "default": true}, ...]}
# input-capable devices only; enumerates via sounddevice.query_devices, never opens the
# mic. The list is PortAudio's snapshot from agent start -- unless "refresh": true, which
# re-initialises PortAudio first so a newly plugged-in device appears. That closes and
# reopens the voice session's mic (~0.1 s gap in wake-word listening), is answered
# asynchronously (same request_id) rather than inline, and fails with ok:false during a
# voice session.

{"type": "list_wake_models", "paths": ["~/models/hey_leutheria.onnx"], "request_id": "5"}
# -> {..., "ok": true, "pretrained": [{"name": "hey_jarvis", "downloaded": true}, ...],
#     "paths": [{"path": "~/models/hey_leutheria.onnx", "exists": false}]}
# names only, no model loaded or downloaded; `paths` (optional) are custom entries to
# check for existence. The openwakeword import (~0.5 s) happens once, then is cached.
```

### Skill proposal protocol

May follow a `response` (after its spoken reply) whenever a multi-step (2+ tool call)
success isn't already covered by an existing skill -- on the first occurrence as well as
repeats, not just repeats. Fully independent of the request/response cycle -- nothing is
waiting on this, so it can arrive or not without affecting anything else:

```python
{"type": "response", "text": "...", "trace": [...]}          # the actual answer, unaffected
{"type": "speaking", "state": "started", ...}                  # if any, then "ended"
{"type": "skill_proposed", "id": "<uuid>", "name": "...", "description": "...",
 "steps": [{"tool": "...", "args": {...}}, ...],
 "uncertain_params": [
   {"name": "...", "guessed_value": "...", "question": "..."}   # empty list if confident
 ]}

# for each uncertain_params entry, optionally answer whether it's a real
# parameter (true) or should be baked in as a fixed constant (false) --
# anything unanswered defaults to true (stays a parameter) on save:
# you respond independently, whenever (or never):
{"type": "skill_response", "id": "<uuid>", "approved": true,
 "resolutions": {"<param_name>": false}}   # or "approved": false, no resolutions needed

# only on approval, and only if it validated and was written:
{"type": "skill_saved", "name": "..."}
# or, if it failed validation on save (also logged as skill_register_rejected --
# see "Generated skill format"); the app shows it as an error in chat:
{"type": "skill_save_failed", "name": "...", "error": "..."}
```

A decline is just logged (`skill_declined` in `agent/logs/events.jsonl`) -- no reply is
sent back for it, since there's nothing further to report. `uncertain_params` is always
present (possibly empty) so the client doesn't need to special-case a missing key.

### Management protocol (what the Library and Permissions views use)

Everything else in the protocol is an event stream. These (plus the voice ones above) are
request/response: each
carries a `request_id` that comes back on the reply, so several can be in flight at once
and interleave freely with a command that's mid-confirmation. They're handled inline in
`server.py`'s read loop via `control.py` — no LLM call, nothing that can block.

```python
{"type": "inventory", "request_id": "1"}
# -> {"type": "control_result", "request_id": "1", "ok": true,
#     "tools":   [{name, kind, description, input_schema, safety, enabled,
#                  requires_confirmation, default_requires_confirmation,
#                  is_overridden, runs, failures, last_used}, ...],
#     "skills":  [{...same, plus origin, steps, source_signature,
#                  created_at, deletable}, ...],
#     "history": [{ts, event: "proposed"|"saved"|"declined", name, ...}, ...],
#     "trusted": [{tool, args, scope: "always"|"session", key}, ...]}

{"type": "set_preference", "name": "run_command", "enabled": false, "request_id": "2"}
{"type": "set_preference", "name": "open_url", "requires_confirmation": true, "request_id": "3"}
{"type": "set_preference", "name": "open_url", "reset": true, "request_id": "4"}
# -> {"type": "control_result", "request_id": "...", "ok": true, "name": "...", ...}

{"type": "delete_skill", "name": "archive_downloads", "request_id": "5"}
# -> ok:false with an explanation for a hand-written skill -- only generated ones delete

{"type": "revoke_trust", "key": "<the `key` field from an inventory `trusted` entry>", "request_id": "6"}
```

`is_overridden` means the *effective* behaviour differs from the built-in default, not
merely that a stored entry exists — an override that happens to restate the default isn't
something you'd want offered as a customization to undo.

One more event, on the stream side: `{"type": "confirmation_resolved", "id", "approved",
"by": "voice"}` is sent when a pending confirmation is answered out-of-band by a spoken
yes/no, so the on-screen card settles instead of sitting there with live buttons for a
question that's already been answered.

## Logs

Every message received, every LLM decision, and every tool call (regardless of whether
it came from the LLM loop or a direct `{"tool": ...}` message) is appended as one JSON
line to `agent/logs/events.jsonl` (gitignored). Event types: `message_in`, `message_out`,
`llm_turn` (what Claude decided that turn), `tool_call` (which tool ran, with what args
and result), `stt_transcript` (what Whisper heard), `tts_error` (a synthesis failure --
non-fatal; `backend` says which engine failed, and `fallback: "piper"` means the reply was
still spoken by Piper), `confirmation_requested`/`tool_declined` (the destructive-tool
confirmation flow), `confirmation_answered_by_voice`, `preference_changed`/
`preference_reset`/`skill_deleted`/`trust_revoked` (management actions taken from the UI),
the skill lifecycle (`skill_proposed`/`skill_registered`/`skill_declined`, plus
`skill_proposal_rejected`, `skill_register_rejected`, `skill_load_rejected` and
`skill_migrated` from format validation -- each rejection carries the reason),
`dispatch_blocked_disabled` (something tried to run a capability you turned off), and the
voice session's own: `wake_detected` (with its score), `voice_session_started`/
`voice_session_ended` (trigger / reason and duration), `voice_transcript` (text, speech
duration, Whisper's `no_speech_prob` per segment, anything dropped, whether it was
filler or a dismissal), `voice_utterance_rejected` (too short to transcribe),
`voice_confirmation_unclear`, `voice_settings_changed`, and `voice_error` (a stage —
`load_models`, `capture`, `transcribe`, `command` — and the error; voice goes to `off`).
The Library's usage counts and the Activity view's history are both derived from this
file, so it's the single source of truth for both. Tail it live while testing:

```bash
tail -f agent/logs/events.jsonl
```

There's no rotation or querying yet — it's intentionally just an append-only file for
now; it's the raw material later work (e.g. detecting repeated requests to propose a
saved skill) will read over.

## Stopping things

- `npm start` running in the foreground: `Ctrl+C` (it kills the Python subprocess too via `before-quit`/`window-all-closed`).
- If you ran the agent standalone or something got orphaned:
  ```bash
  pkill -if "python -m agent"
  pkill -f "Electron.app"
  ```

## Troubleshooting

**The voice indicator says `off` and "hey Jarvis" does nothing**

Look for the latest `voice_error` in `agent/logs/events.jsonl` — its `stage` says what
failed. `capture` is the mic: no input device, the configured `input_device` doesn't
exist, or microphone permission was denied (System Settings → Privacy & Security →
Microphone, for Electron or your terminal). `load_models` is openWakeWord: a
`wake_models` entry that isn't a pretrained name or an existing `.onnx` path, or the
first-run model download failed (it needs network once). Changing any voice setting
retries.

**`OSError: ... address already in use` on port 8765 when running `npm start`**

A previous agent process (e.g. from a force-quit or crash) is still holding the port.
`main.js` auto-kills any stale `python -m agent` process before spawning a new one on
every `npm start`, so this should self-heal. If you still see it, the auto-kill's
`pkill` pattern may not be matching your Python's resolved binary name — kill it
manually and retry:
```bash
pkill -if "python -m agent"
npm start
```
