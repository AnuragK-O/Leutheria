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
        |  (authenticated: a per-launch token, checked in the handshake)
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
  agent-auth.js        The agent socket's per-launch token: generated here, handed to the
                       sidecar and the Qt overlay in their environment, sent as a header
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
    mock-fixtures.js   Its fixture catalog, history, approvals, devices (for the non-chat views)
    ui-capture.js      Drives the real app against the mock: screenshots + behaviour checks
    auth-check.js      Checks agent-auth.js, what `ws` puts on the wire, and the mock's
                       token enforcement (no Electron, no Python)
    make-mark.js       Regenerates renderer/img/mark-*.png from assets/1.png
                       and the Qt overlay (understands `hello` / the overlay role)

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
    overlay_protocol_check.py  The overlay client role: real server on a side port,
                       a UI and an overlay client in both orders -- never primary,
                       mirrored messages, confirming from the overlay (no API key needed)
    ws_auth_check.py   The socket's authentication, on the raw handshake of the real
                       server on a side port: 401 without the token, 403 with an Origin,
                       the token never logged, standalone token file (no API key needed)
    skills_check_dev.py  Dev / web / clipboard tools and skills through the real
                       dispatch(): temp HOME, stubbed browser/app/clipboard, fetch_url
                       against a local http.server, git in temp repos (no API key needed)
    skills_routing_check.py  REAL MODEL, costs money: ~140 spoken phrasings, each sent as
                       one first turn with the real tool list; records which capability
                       the model reaches for. Nothing is dispatched. Cached per phrase.
    skills_routing_cache.json  That check's cached answers (committed, so --report is free)
    skills_e2e_check.py  REAL MODEL, costs money: 18 requests (with follow-ups and
                       refusals) through agent_loop + dispatch in a sandbox home;
                       --scripted replays written plans with no model and no cost
    dispatch_loop_check.py  Tools off the event loop: the loop stays responsive during a
                       slow tool, "thread": "main" tools stay on it, run_command's timeout /
                       kill / output cap, the confirmation timeout message (no API key needed)
  .venv/              Virtualenv (gitignored)
  agent/
    core/
      ws_auth.py       Who may open the socket: the token (from LEUTHERIA_TOKEN, or
                       generated into a 0600 file standalone) and the handshake check
                       that refuses a missing/wrong token (401) or any Origin (403)
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
      fetch_url.py       git_status.py       open_with.py        (dev/web)
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
      dev_common.py       Shared by the dev/web/clipboard skills below: the Steps runner
                        (every step through dispatch()), argument checks, shell quoting,
                        the never-overwrite scaffold helper. Not a skill itself
      new_python_project.py  new_node_project.py  new_web_page.py   (scaffolds)
      git_snapshot.py    git_sync.py    clone_repo.py    repo_overview.py
      open_workspace.py  github_open.py  code_scratchpad.py
      web_search.py      research_topic.py  define_term.py
      clipboard_to_note.py  clipboard_to_file.py  save_page_notes.py  read_later.py
                        Dev / web / clipboard skills -- see "Skills" for the table
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

Seven views in the left sidebar:

| view | what it's for |
|---|---|
| **Assistant** | The conversation. Type or talk; confirmations and skill proposals appear inline as cards. |
| **Library** | Every tool and skill (90-odd) in one master/detail list, grouped by what it's for (Files, Search and storage, Tidy and archive, Notes and clipboard, Web and weather, Development, System, Apps and GUI control, Timers and routines, Other). Groups fold away and stay folded; the filter matches name and description and highlights the hit; four state filters (asks first, disabled, learned, never used) narrow the list. Keyboard: `/` filter, arrows move and the detail follows, left/right fold a group, Esc clears. Select one to read what it does (first sentence) and when the model is told to use it (the rest, one sentence per line, other capabilities linked), see usage, turn it off, make it always ask first, or (for a learned skill) inspect its steps and delete it. Groups are assigned in `app/renderer/js/library-groups.js`; anything unlisted lands in Other rather than disappearing. |
| **Activity** | How the skill set has grown: every skill proposed, saved, or turned down, plus a most-used breakdown. |
| **Permissions** | Standing approvals (the `remember: session/always` ones) with a revoke button, and every capability whose settings differ from its default, with a reset. |
| **Voice** | Settings for the always-listening session: wake word on/off, which wake words, sensitivity, the end-of-utterance pause, the silence timeout, dismiss phrases, and the microphone. See below. |
| **Overlay** | Everything that floats outside this window: the hot edge, where the chat panel and alerts appear, appearance (theme, accent, opacity, density) and each panel's saved layout. See "Overlay panels" below. |
| **Logs** | The raw bridge stream, out of the way of the conversation, one row per line with its source tag tinted and stderr (`:err`) marked red. Copy or clear it. |

Dark is the default theme; a light one ("paper terminal") ships too, and "system" follows
macOS. Toggle at the bottom of the sidebar or pick in the Overlay view; the choice is saved
with the other appearance settings and every window (main, overlay, panels) follows it.

**Design language.** The window is a control surface for a machine that acts on your
computer, so it shows state rather than decorating: near-black ground with a violet cast,
1 px hairlines, no rounded corners and no shadows, a cut corner on controls and on the
two cards that wait on you (a confirmation, a skill proposal), which also carry corner
brackets. Colour only ever means state: violet (from the logo) is brand and selection,
cyan is live (connected, mic open, switched on), amber is waiting on you (an approval),
red failed or destroys, green succeeded. Two typefaces: Chakra Petch for what a person
reads, JetBrains Mono for what the machine said (tool names, arguments, paths, counts,
times, logs). The transcript is a session log (who and when in a left column, tool calls
as mono rows with an OK or FAILED status), not chat bubbles. The activation overlay uses
the same tokens and primitives.

| what | where |
|---|---|
| Every token: colour for both themes, type scale, spacing, cut sizes, motion | the first two blocks of `app/renderer/styles.css` |
| Token table, usage rules, and the overlay spec the Qt overlay is ported from | `app/renderer/DESIGN.md` |
| Fonts (woff2, bundled; nothing is fetched at runtime) | `app/renderer/fonts/` |
| Logo mark, downsized and made transparent for use as a CSS mask | `app/renderer/img/mark-{64,128,256,512}.png` (regenerate: `cd app && env -u ELECTRON_RUN_AS_NODE npx electron scripts/make-mark.js`) |
| Icons (stroked SVG paths; no emoji in UI strings) | `PATHS` in `app/renderer/js/util.js` |

Font licences: Chakra Petch (Copyright 2018 The Chakra Petch Project Authors) and
JetBrains Mono (Copyright 2020 The JetBrains Mono Project Authors) are both under the SIL
Open Font License 1.1; the licence texts are next to the fonts
(`fonts/ChakraPetch-OFL.txt`, `fonts/JetBrainsMono-OFL.txt`). The dock and tray icons are
unchanged (`assets/4.png`, `assets/1.png`).

**Checking the UI.** `cd app && node scripts/ui-capture.js --out <dir>` starts its own
mock agent and its own Electron (isolated userData, never port 8765), drives every view
and state in both themes at 1180 and 940 px wide, checks on the way that each control
still does what it did (typed command, confirmation answered from the main window and
from the overlay, skill proposal, Library toggle and Reset, Revoke, Voice settings save,
refusal and Revert, theme toggle, the voice pill, toasts, a confirmation that times out
(`by: "timeout"`) and one that needs a click (`voice_approvable: false`), a settings file
the agent couldn't read, the delete flow for a learned skill, a 200-entry transcript,
non-Latin text, reduced motion, Logs Copy and Clear, and the agent going away), and
writes a PNG of each. A second pass per theme covers the overlay panels: each opened by a
`panel` message (the mock sends them for "show me ..." commands), queries, an unknown id,
hide and hide-all, the controls inside panels, both orientations, dock/move/resize/opacity/
pin and the layout file, persistence across a restart, a corrupt and a partly bad file, the
hot edge (the cursor and mouse button are injected through an env-gated test hook,
`LEUTHERIA_TEST_HOOKS=1`, that exists only in that run), auto-hide, both alerts anchors,
and accent/density in every window; and that no panel ever becomes the focused window or
makes the app frontmost. `--panels-only` / `--no-panels` pick one pass. One
line per check, non-zero exit on a failure. The PNGs are for looking at: a UI change isn't
verified until someone has. `--real ws://127.0.0.1:<port>` runs a reduced pass against an
agent that's already running, sending only management messages. The windows appear on
screen while it runs.

**Overlay panels.** Any of six panels can float over other apps: `chat` (the transcript,
composer and confirmation cards, live-synced with the main window), `skills` (the
Library's grouped list with its filter and a short detail with the two switches),
`activity`, `permissions` (Revoke works), `timers` (a countdown per timer; Cancel asks
Leutheria to cancel it, so it runs through cancel_timer like any other request) and
`voice` (the session's state, last thing heard and said, start/stop). They open three
ways: by asking ("show me my skills", "what timers are running" -- the agent's
`show_panel`/`hide_panel` skills send a `panel` message), from the Overlay view, or with
the **hot edge**: rest the cursor against a screen edge (default: right, 350 ms) and the
configured panel (default: chat) slides in docked to that edge. A panel never takes focus
when it appears (it's an NSPanel shown inactive); click into the chat panel to type.

Each panel has a heading you drag to move it (moving undocks it: it stays where you drop
it), resize edges (only the inner one while docked), and a control strip: dock (left,
right, top, bottom, free), vertical/horizontal (horizontal is a real strip layout: the
transcript as columns, timers as cards, the Library's groups side by side), opacity, pin
(a pinned panel never hides itself and comes back when the app starts) and close.
Unpinned panels hide themselves once the cursor has been over them and left (or after a
delay, or never: per panel, in the Overlay view). The layout of every panel, the hot edge,
the alerts anchors and appearance are saved in Electron's userData as
`overlay-layout.json` (`~/Library/Application Support/app/` for a dev run), validated
field by field on load: a bad field falls back to its default with a log line, and a file
that isn't JSON is moved aside to `overlay-layout.json.corrupt`. Positions are clamped to
the screen's work area; a panel left on a display that's since been unplugged reopens on
the main one.

Hot edge notes: the cursor is polled (Electron has no global mouse-move event) every 80 ms,
only while the hot edge is on or an auto-hiding panel is up. It doesn't fire while a mouse
button is held (dragging a window or file to that edge; asked of AppKit once per dwell), it
re-arms only after the cursor leaves the edge, and an edge shared with another monitor
doesn't count (the cursor passes through it). The top edge is where macOS reveals the menu
bar in a full-screen app, and the Dock's edge (if it auto-hides) slides the Dock out too:
the Overlay view says so when you pick one of those.

**Alerts and appearance.** The activation overlay (and its approval card) can sit at any of
seven anchors (top left/center/right, center, bottom left/center/right) on the cursor's or
the main display; on a bottom anchor the card opens above the plate. Toasts take their own
anchor inside the window that raised them. The native Qt overlay (`LEUTHERIA_OVERLAY=qt`)
keeps top-center. Appearance: theme, density (comfortable/compact; overrides spacing tokens
only), default panel opacity (60-100%), whether panels float above other windows, and an
**accent** that replaces the brand violet (presets or any colour). The accent is checked in
both themes against DESIGN.md's contrast bar (4.5:1 for text and for white text on the
primary button, 3:1 for marks) and nudged lighter or darker where it falls short, with a
note saying what changed; one that would read as a state colour (cyan, green, amber, red)
is refused, because those colours mean live, succeeded, waiting and failed and aren't
editable.

**Confirmation cards** show the agent's `reason` (why this one asks) when it sends one,
and when `voice_approvable` is false they carry an amber NEEDS A CLICK line in place of
"say yes or no": a spoken yes won't approve it. A confirmation the agent times out
(`confirmation_resolved` with `by: "timeout"`) settles as "Timed out with no answer. It
wasn't run." If the inventory carries `settings_errors` (a settings file the agent
couldn't read), a red notice heads the Library and Permissions views.

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

**The native Qt overlay.** `overlay-qt/` is a C++/QML implementation of the same overlay
(build: `cmake -S overlay-qt -B overlay-qt/build -G Ninja
-DCMAKE_PREFIX_PATH=/opt/homebrew/opt/qt && cmake --build overlay-qt/build`). It's a separate process and a second client of the
agent's WebSocket, not part of Electron. `LEUTHERIA_OVERLAY=qt` makes `main.js` skip its
own overlay window and instead spawn
`overlay-qt/build/LeutheriaOverlay.app/Contents/MacOS/LeutheriaOverlay --url <agent url>
--assets app/renderer/overlay/assets`, and kill that child on quit. If the binary isn't
built (or won't start, or exits early), it logs a line saying so and falls back to the
Electron overlay, so you never end up with no overlay at all. `LEUTHERIA_QT_OVERLAY_BIN`
overrides the binary path. The tray, ⌥Space and the header voice indicator don't depend
on which overlay is in use. Unset, the Electron overlay is used, as before.

```bash
cd app && env -u ELECTRON_RUN_AS_NODE LEUTHERIA_OVERLAY=qt npm start
```

**Visual assets** live in `app/renderer/overlay/assets/`. `manifest.json` maps each state
to a built-in CSS placeholder, an image (png/svg, or animated WebP — the recommended animated format, since it plays in
both overlays) or a video (webm/mp4, Electron overlay only for webm);
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
| `LEUTHERIA_TOKEN` | With `LEUTHERIA_NO_SPAWN=1`: the token of the agent being connected to (see "Authentication"). Unset, the app reads the token file a standalone agent wrote for that port. Ignored when the app spawns the agent itself — it always generates a fresh one |
| `LEUTHERIA_CAPTURE_DIR` | Debug only: write a PNG of the overlay and main window on every voice-state change, into this folder (which also gets its own `userdata/`, so a debug run never shares storage with a real one) |
| `LEUTHERIA_CAPTURE_THEME` | With the above: force the overlay to `light` or `dark` |
| `LEUTHERIA_OVERLAY=qt` | Use the native Qt overlay instead of the Electron one (see above); falls back if it isn't built |
| `LEUTHERIA_QT_OVERLAY_BIN` | With the above: the Qt overlay binary to spawn, instead of the `overlay-qt/build/` one |

```bash
cd app
node scripts/mock-agent.js &
env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
```

The mock refuses a handshake that carries an `Origin` header, like the real agent. It only
checks a token if it is started with `LEUTHERIA_TOKEN` set — give the app the same value
(`ui-capture.js` does this with a fresh token every run, so the app's real authentication
path is exercised):

```bash
export LEUTHERIA_TOKEN=$(openssl rand -hex 32)
node scripts/mock-agent.js &
env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
```

`node scripts/auth-check.js` checks the token plumbing on its own (what the sidecar is
handed, what goes on the wire, the mock's enforcement) without starting Electron.

`MOCK_LOOP=1` replays the session after each one ends; `MOCK_AUTOSTART=0` waits for
⌥Space or the menu instead; `MOCK_CONFIRM_TIMEOUT_MS` sets how long it waits for a click.

The mock runs one session shared by every connection and routes messages the way the
real agent does (see "Overlay clients" in the protocol section), so the Qt overlay can be
developed against it, with or without Electron:
`overlay-qt/build/LeutheriaOverlay.app/Contents/MacOS/LeutheriaOverlay --url ws://127.0.0.1:8799`.
In one way it's more lenient than the real agent: with only an overlay connected, the
scripted confirmation still shows up on the overlay. The real agent turns a confirmation
down automatically when no UI is connected.

## Running the Python agent standalone (no Electron)

Useful for testing tools directly without going through Electron.

```bash
cd agent
./.venv/bin/python -m agent
```

This starts the WebSocket server at `ws://127.0.0.1:8765` and prints one line once it's
up (`LEUTHERIA_PORT=8781` picks another port, to run beside the app):

```
AGENT_READY ws://127.0.0.1:8765 auth=token-file:/Users/you/Library/Application Support/Leutheria/agent-8765.token
```

With voice enabled it also opens the microphone; `LEUTHERIA_VOICE_SOURCE=/path/to.wav`
feeds that WAV in instead (at real speed, then silence), so the voice session can be driven
without one.

### Authentication

Every connection is authenticated during the WebSocket opening handshake, before the
upgrade — a client that fails never gets a connection, never becomes the primary and never
sees a frame. Two rules (`core/ws_auth.py`), with no switch to turn either off:

1. **A request with an `Origin` header is refused (403)**, whatever else it carries.
   Browsers always send `Origin` on a WebSocket handshake and page script can't remove it;
   none of the real clients send one (Node's `ws`, Qt's `QWebSocket` and Python's
   `websockets` all leave it out unless asked). No value is allowed — not `null`, not
   `file://`, not the agent's own address.
2. **The request must carry `Authorization: Bearer <token>` (401 otherwise)**, compared in
   constant time. A header rather than a query string, because URLs end up in logs and a
   browser can't set this header on a WebSocket at all.

Without both, loopback is not a boundary: any local process — or JavaScript on any web page
open in a browser — could connect to `127.0.0.1:8765`, send a command, receive the
`confirmation_required` and approve it itself.

Where the token comes from:

| how the agent was started | token | who knows it |
|---|---|---|
| by the app (`npm start`) | Electron generates 32 random bytes on every launch and passes them in the sidecar's environment (`LEUTHERIA_TOKEN`) | the Electron main process, the sidecar, and the Qt overlay if `LEUTHERIA_OVERLAY=qt` (also by environment). Never a renderer, a file, a log or a command line |
| standalone (`python -m agent`, no `LEUTHERIA_TOKEN`) | the agent generates its own on every start and writes it to `~/Library/Application Support/Leutheria/agent-<port>.token` — mode 0600, in a 0700 directory | any process running as you can read the file; nothing else can. `LEUTHERIA_RUNTIME_DIR` moves the directory (the checks use it); the agent refuses to start if it's a symlink, not yours, or writable by others |
| standalone with `LEUTHERIA_TOKEN` set (the check scripts) | that value; at least 32 characters or the agent refuses to start | whoever set it |

The agent removes `LEUTHERIA_TOKEN` from its own environment as soon as it has read it, so
a command it runs can't see it. The `AGENT_READY` line says which mode is active
(`auth=token-env` or `auth=token-file:<path>`) and never the token; refused handshakes are
logged as `ws_rejected` with a reason (`origin` / `no_token` / `bad_token`) and nothing
else, at most one line per reason every 10 seconds.

What this does and doesn't protect against: it stops web pages and other users' processes
completely, and stops a same-user process that merely knows the port. It does not stop a
process running as you that goes looking — it can read the token file, or the environment
of the Electron/agent process — because macOS doesn't isolate one of your processes from
another. That is the same position as any other file you own.

**Connecting a debugging client.** Read the token and send the header:

```bash
cd agent
./.venv/bin/python -c "
import asyncio, json, websockets
from agent.core import ws_auth

async def main():
    token = ws_auth.read_token_file(8765)          # the standalone agent's token file
    async with websockets.connect('ws://127.0.0.1:8765', additional_headers=ws_auth.bearer(token)) as ws:
        await ws.send(json.dumps({'type': 'inventory', 'request_id': '1'}))
        print((await ws.recv())[:200])

asyncio.run(main())
"
```

To point the app or the Qt overlay at a standalone agent, give them the same token:
`LEUTHERIA_TOKEN="$(cat ~/Library/Application\ Support/Leutheria/agent-8765.token)"` in
their environment (the app with `LEUTHERIA_NO_SPAWN=1` finds that file by itself; the Qt
overlay does not). A browser-based WebSocket tool can't connect at all: it sends `Origin`.

`./.venv/bin/python scripts/ws_auth_check.py` proves all of the above against the real
server on a side port.

### Manual tool dispatch (debugging only)

A message with a `tool` field runs that one tool directly, with no model in between. No
real client sends it — the app sends text and audio — so it is **off unless the agent is
started with `LEUTHERIA_MANUAL_DISPATCH=1`**; otherwise the reply is
`{"type": "error", "text": "manual tool dispatch is disabled ..."}` and
`manual_dispatch_refused` is logged. The snippets below that send `"tool"` assume it is on.
It doesn't bypass anything when enabled (the same `dispatch()`, tiers, preferences and
confirmation gate apply), but it is the shortest route from a connection to a tool, so it
isn't left open for nobody.

```bash
cd agent
LEUTHERIA_MANUAL_DISPATCH=1 ./.venv/bin/python -m agent
# then, from the client above:
#   await ws.send(json.dumps({'type': 'command', 'tool': 'list_files', 'args': {'path': '~/Desktop'}}))
```

### Available tools

| tool | args | safety | notes |
|---|---|---|---|
| `open_app` | `{"name": "Calculator"}` | safe | an installed app by NAME: the name is matched (case-insensitive, `.app` optional, or the bundle's own display name) against bundles in `/Applications`, `/System/Applications`, their Utilities folders, CoreServices and `~/Applications`, and `open -a <resolved bundle path>` is run with nothing after it. Refuses paths, `..`, a leading `-`, control characters, a bundle whose real path is outside those folders, and any name that doesn't resolve |
| `create_folder` | `{"path": "~/Desktop/test"}` | safe | `mkdir -p` equivalent |
| `list_files` | `{"path": "~/Desktop"}` | safe | lists directory contents |
| `run_command` | `{"cmd": "echo hi"}` | **destructive** | pauses for a live user confirmation before it runs — see "Confirmation flow" below. Optional `timeout_s` (default 300, max 1800); output is capped — see "Where tools run, and `run_command`'s limits" |
| `get_clipboard` | `{}` | safe | reads clipboard text |
| `set_clipboard` | `{"text": "..."}` | safe | copies text to clipboard |
| `open_url` | `{"url": "https://..."}` | safe | web pages only: `http`/`https` with a host, run as `open -- <url rebuilt from its parsed parts>`. Refuses paths, `file:`, every other scheme (`shortcuts:`, `ssh:`, `smb:`, `vnc:`, `x-apple.*`, `vscode:`, `tel:`, `mailto:`, `javascript:`, `data:`, ...), `user:pass@`, whitespace/control/invisible characters, backslashes and a leading `-`. Local files go through `open_file` |
| `show_notification` | `{"title": "...", "message": "..."}` | safe | macOS notification banner |
| `take_screenshot` | `{"save_path": "..."}` (optional) | safe | defaults to a timestamped file on the Desktop; never overwrites — an existing `save_path` is refused |
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
| `get_volume` | `{}` | safe | output volume 0–100 and mute state (`osascript`, no Automation permission) |
| `set_volume` | `{"level": 30}` / `{"change": -15}` / `{"muted": true}` | safe | reversible; returns the previous level and mute state; a level change never unmutes on its own |
| `media_control` | `{"action": "play_pause"}` (`next`, `previous`) | safe | presses a system media key (Quartz event, needs Accessibility); can't tell whether anything was playing |
| `get_system_info` | `{}` | safe | CPU load, memory, uptime, macOS version, model — read-only `sysctl` / `vm_stat` |
| `get_network_status` | `{}` | safe | connected? internet reachable? interface, local IP, router. No public-IP lookup |
| `get_weather` | `{"place": "Austin, Texas", "days": 3}` (all optional) | safe | Open-Meteo, no key; no place = saved home location, neither = fails and says to ask |
| `set_home_location` | `{"place": "Paris"}` | safe | saves one geocoded place to `agent/home_location.json`; returns the previous one |
| `set_timer` | `{"minutes": 10, "label": "pasta"}` | safe | in-process countdown; banner + spoken line when it ends; lost on restart; max 24 h, 20 at once |
| `list_timers` | `{}` | safe | running timers, soonest first |
| `cancel_timer` | `{"timer_id": "3"}` / `{"label": "pasta"}` / `{"all": true}` / `{}` | safe | `{}` works only when exactly one is running |
| `open_file` | `{"path": "~/Documents/resume.pdf"}` | safe | default app, documents/media/folders only — refuses apps, scripts, programs, installers, unknown types, anything with an execute bit or Mach-O/ELF header, packages that aren't documents, and types that act when opened (`.wflow`, `.webarchive`, `.playground`, macro-enabled Office files, `.crx`, `.jnlp`, location files, `.code-workspace`). Shares its classifier with `open_with` |
| `reveal_in_finder` | `{"path": "..."}` | safe | `open -R`: selects in Finder, never opens |
| `find_files` | `{"path": "~/Downloads", "extension": "pdf", "modified_within_days": 2}` | safe | read-only search by name / extension / min size / age; never follows symlinks; skips hidden files, `node_modules` and app bundles; at most 500 results, 10 s / 200,000 entries (then `incomplete: true`) |
| `file_info` | `{"path": "..."}` | safe | read-only; kind, size, created/modified. A missing path is `exists: false`, not an error |
| `folder_size` | `{"path": "~/Downloads", "limit": 10}` | safe | read-only total plus the largest immediate items; counts hidden files; a lower bound (`incomplete: true`) past 15 s / 500,000 entries |
| `zip_folder` | `{"source": "...", "destination": "...", "exclude": ["node_modules"]}` | safe | source only read; archive created with an exclusive open, so an existing zip is never replaced; symlinks left out; capped at 20,000 files / 1 GB |
| `extract_archive` | `{"archive": "...", "destination": "..."}` | safe | zip and tar(.gz/.bz2/.xz) into a folder it creates itself — refuses an existing destination; every entry name validated first (zip-slip refuses the whole archive); links never created; capped at 20,000 entries / 2 GB / 120 s (the deadline is checked inside a long tar member too). Nothing comes out executable (execute and setuid bits are never applied), and a downloaded archive's `com.apple.quarantine` value is copied onto every extracted file and folder |
| `find_duplicates` | `{"path": "...", "min_size_kb": 1}` | safe | read-only, report only; same size, then first 64 KB, then full-content hash; largest waste first |
| `write_new_file` | `{"path": "...", "content": "..."}` | safe | create-only (`open(..., "x")`): refuses if anything exists at the path, including a symlink. Returns `{"ok": true, "path": ...}` |
| `search_file_contents` | `{"path": "...", "text": "wifi password"}` | safe | read-only literal, case-insensitive search of text files under 2 MB; binary files skipped |
| `spotlight_search` | `{"query": "resume", "folder": "..."}` (folder optional) | safe | read-only `mdfind -name` with fixed argv and an 8 s timeout; names only, newest first |
| `fetch_url` | `{"url": "https://...", "max_chars": 8000}` (`max_chars` optional) | safe | dev/web. One GET of a public http(s) page, returned as readable text (tags, scripts, menus and footers stripped). Read-only and bounded: public addresses only (loopback/private/link-local refused on every redirect hop), NAT64/6to4/IPv4-compatible addresses judged by the IPv4 address they carry, 5 redirects, 12 s overall as an absolute deadline over lookup, connect, TLS, headers and body (a watchdog closes the sockets), 1 MB read, 20,000 chars returned, text content types only. Reports `content_type`, `truncated`, final `url`. For reading a page, not showing one — that's `open_url` |
| `git_status` | `{"path": "~/Projects/atlas", "log_count": 5}` (`log_count` optional) | safe | dev/web. Branch, ahead/behind, staged/modified/untracked files, recent commits, origin URL (credentials stripped). Fixed argv, 10 s timeout, never refreshes the index, and programs named by the repository's own config are switched off (every `filter.<name>` defined, includes followed, is overridden to nothing; `core.fsmonitor`; `gpg.program`; hooks; submodules ignored; pagers pinned to `cat`), so it stays a read on a repo someone else made |
| `open_with` | `{"path": "~/Projects/atlas", "app": "Visual Studio Code"}` | safe | dev/web. `open -a App path` (fixed argv) for an existing folder or content file. Symlinks are resolved first. Refuses apps that execute, install or mount what they're handed (`ui_access.DENIED_APPS` plus terminals, script runners, launchers, installers, archive/disk-image apps); Finder gets folders only. The path is judged by `open_file`'s classifier (by UTI, plus a suffix backstop on every parent), with two allowances for the named app: plain source files (`.py`, `.js`, ...; never shell scripts) and undeclared text files (`Makefile`, `.gitignore`). So it refuses `.command`, `.sh`, `.app` (and anything inside a bundle), `.jar`, `.pkg`, `.dmg`, `.webloc`/`.afploc`/`.vncloc`, `.jnlp`, macro-enabled Office files, `.code-workspace`, `.wflow`, `.playground`, packages such as `.wdgt`/`.mdimporter`/`.download` even into Finder, any file with an execute bit, compiled binaries, extensionless scripts |

Example calls for each (manual dispatch — see above; needs `LEUTHERIA_MANUAL_DISPATCH=1`):

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
VS Code), each requiring its own approval. All three go through `dispatch()`, the project
name must be a plain folder name (the same rule the scaffold skills use), and the path is
`shlex`-quoted in both commands.

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

#### System and productivity skills

Ten more hand-written skills, all composed of safe tools (so none of them prompts unless
you mark one of their steps "ask first" in the Library, in which case that step pauses
inside the skill like any other). Each stops at the first failed step and its error names
the step and what had already been done.

| skill | steps (each a `dispatch()`) | try saying |
|---|---|---|
| `morning_briefing` | `get_current_datetime` → `get_battery_status` → `get_disk_space` → `get_weather` (skipped if no place and no home location) | "give me my morning briefing" |
| `system_health_check` | `get_system_info` → `get_disk_space` → `get_battery_status` → `get_network_status` | "how's my Mac doing" |
| `weather_report` | `get_weather` (2–7 days, read out day by day) | "what's the weather this week" |
| `quick_timer` | `set_timer` → `show_notification` (parses "10 minutes", "1h30m", "an hour and a half") | "set a timer for ten minutes" |
| `pomodoro` | `set_timer` (focus) → `set_timer` (break) → `show_notification` | "start a pomodoro" |
| `focus_mode` | `get_volume` → `set_volume` (mute) → `set_timer` → `show_notification` | "I need to focus for half an hour" |
| `meeting_prep` | call: `get_network_status` → `get_volume` → `set_volume` (if muted/quiet) → `get_battery_status` → `open_app`? → `set_timer`?; in person: mutes instead | "I'm heading into a meeting" |
| `end_of_day` | `get_current_datetime` → `list_timers` → `get_battery_status` → `get_disk_space` → `get_system_info` → `get_weather` (if home is saved) — read-only | "I'm done for the day" |
| `wind_down` | `get_volume` → `set_volume` (only ever lower) → `set_timer` → `show_notification` | "I'm winding down" |
| `battery_check` | `get_battery_status` → `show_notification` (only if low and not charging) | "do I need to plug in" |

**Timers** live in `agent/core/timers.py` as asyncio tasks, so they last exactly as long
as the agent process — nothing is persisted, and the tool results say so. When one ends it
shows a banner through `dispatch("show_notification")` (so disabling that tool silences
timer banners too) and speaks a line through `tts.speak()` on the primary connection, or
with no UI connected at all. The countdown is against the wall clock, so a timer whose time
passed while the Mac slept goes off within a few seconds of waking rather than late by the
length of the nap. Events: `timer_set`, `timer_cancelled`, `timer_fired`.

**Weather** uses [Open-Meteo](https://open-meteo.com) (no key). Location only ever comes
from the request or from a home location the user asked to have saved
(`agent/home_location.json`, gitignored) — there is no IP geolocation. Set
`LEUTHERIA_WEATHER_URL` / `LEUTHERIA_GEOCODE_URL` to point it at another server.

**`focus_mode` is not macOS Focus / Do Not Disturb** — it mutes sound and starts a timer.
Toggling the real setting needs System Events scripting and its permission prompt.

```bash
cd agent && ./.venv/bin/python scripts/skills_check_system.py
```

checks all of the above without changing the volume, pressing a media key, showing a banner,
speaking, or opening a window (each of those is stubbed; weather runs against a local
server with canned JSON, plus one real request that is skipped offline).
#### File and workspace skills

Fifteen hand-written skills over the file tools above. Every step of each one, including
the read-only planning steps, goes through `dispatch()` via `skills/files_common.py:StepLog`;
none imports a tool function or does its own file I/O. All of them compose only safe tools,
so none prompts by default. The check is `agent/scripts/skills_check_files.py`, which runs
them through the real `dispatch()` inside a temp home folder.

| skill | steps | try saying |
|---|---|---|
| `organize_folder` | `file_info` → `find_files` → `file_info`/`list_files` per type folder → `create_folder` → `move_file` × n | "sort my Downloads folder by type" |
| `organize_by_date` | same, one folder per month modified | "group that folder by month" |
| `clean_desktop` | same, into `Desktop/Desktop Cleanup/<type>` | "clean up my desktop" |
| `collect_screenshots` | same, screenshots only, into `~/Pictures/Screenshots` | "put my screenshots away" |
| `archive_old_downloads` | `find_files` (older than n days) → ... → `move_file` × n into `Downloads/Old Downloads/<YYYY-MM>` | "clear out my old downloads" |
| `archive_folder` | `file_info` → `file_info` (free name) → `zip_folder` | "zip up the project folder" |
| `project_snapshot` | `file_info` → `zip_folder` (no dependencies, build output or `.git`) → `append_text` | "snapshot this project before I refactor" |
| `find_large_files` | `folder_size` → `find_files` by size | "what's taking up space in Downloads" |
| `find_recent_files` | `file_info` + `find_files` for each of Downloads, Desktop, Documents | "where's that PDF I downloaded yesterday" |
| `storage_report` | `get_disk_space` → `list_files` → `folder_size` × n | "what's using up my disk" |
| `folder_summary` | `find_files` → `folder_size` | "what's in my Documents folder" |
| `daily_journal` | `file_info` → `write_new_file` (only if absent) → `append_text` → `open_file` if registered | "start today's journal" |
| `screenshot_to_folder` | `create_folder` → `file_info` (free name) → `take_screenshot` | "take a screenshot and file it" |
| `duplicate_report` | `find_duplicates` → `file_info` → `write_new_file` (report in `~/Documents/Leutheria Reports`) | "are there duplicates in my Downloads" |
| `extract_latest_download` | `find_files` (newest archive) → `file_info` (free name) → `extract_archive` | "unzip the thing I just downloaded" |

The five that move files in bulk (`organize_folder`, `organize_by_date`, `clean_desktop`,
`collect_screenshots`, `archive_old_downloads`) share one routine,
`files_common.move_into_folders()`, and so share its guarantees: only `create_folder` and
`move_file` are ever called, a name already taken in the destination gets a ` (2)` suffix
(compared case-insensitively) rather than being overwritten, nothing is deleted, only
visible regular files at the top level of the folder are candidates (no subfolders, hidden
files, links, or downloads still in progress), at most 200 files move per run and the reply
says when that limit was the reason it stopped, and `dry_run: true` performs reads only and
reports the plan. `organize_folder` and `organize_by_date` refuse the home folder, system
folders, `~/Library`, anything inside an `.app`, and git repositories. A skill's result
carries at most 25 step records (`steps_total` has the real count), so a 200-file run does
not put 200 records into the model's context or the event log.

`LEUTHERIA_FILES_HOME=<dir>` makes these skills treat `<dir>` as the home folder when they
resolve Desktop, Downloads, Documents and Pictures, for an end-to-end run that must not
touch the real ones.
#### Dev / web / clipboard skills

Seventeen hand-written skills, one module each in `agent/skills/`, sharing
`dev_common.py` (argument checks, the `Steps` runner that sends every step through
`dispatch()`, and the scaffold helper). A skill stops at the first failed step and its
`message` says which. "Asks" below means a `run_command` step, which the user approves
exactly as if the model had called it; everything else is built from safe tools and runs
in one shot.

| skill | what it does | steps | asks? | try saying |
|---|---|---|---|---|
| `new_python_project` | folder with a runnable `main.py`, README, `requirements.txt`, `.gitignore` | `create_folder`, `write_new_file` x4, then optional `run_command` (git init), `run_command` (venv), `open_with` | only the opt-in git init / venv, once each | "start a new Python project called atlas" |
| `new_node_project` | folder with `package.json`, a runnable `index.js`, README, `.gitignore` | `create_folder`, `write_new_file` x4, then optional `run_command` (git init), `run_command` (npm install), `open_with` | only the opt-in git init / npm install | "make a node project called api server" |
| `new_web_page` | folder with `index.html`, `styles.css`, `script.js` wired together | `create_folder`, `write_new_file` x3, optional `open_url` (the file), `open_with` | no | "create a web page called portfolio and open it in the browser" |
| `git_snapshot` | stage everything and commit with a message; stops on a clean tree, merge conflicts, or a changed file that looks like a secret (`.env`, `*.pem`, `id_rsa`, ...) | `git_status`, `run_command` (`git add -A && git commit -m`) | the commit | "commit everything in atlas with message fix login" |
| `git_sync` | pull (`--ff-only`) or push the current branch; stops with no `origin` or on a detached HEAD | `git_status`, `run_command` | the pull / push | "push my changes in atlas" |
| `clone_repo` | clone `owner/name` from GitHub, or any https URL, into a folder that must not exist yet | `create_folder` (parent), `run_command` (`git clone --`), optional `open_with` | the clone | "clone torvalds slash linux" |
| `repo_overview` | git state, recent commits, project type, top-level listing, start of the README | `git_status`, `list_files`, `read_file` | no | "what's the state of this repo" |
| `open_workspace` | open an existing folder in an editor (VS Code by default) and report its branch | `list_files`, `open_with`, `git_status` if it's a repo | no | "open that folder in VS Code" |
| `web_search` | open a results page: Google by default, or DuckDuckGo, Bing, YouTube, Wikipedia, Maps, Images, News, Scholar, Reddit, Stack Overflow, GitHub. No query means the clipboard's text | optional `get_clipboard`, `open_url` | no | "search the web for mechanical keyboards" |
| `research_topic` | open a few starting points as tabs (web, Wikipedia, news by default) and say which. Four tabs at most, whatever is asked | `open_url` x1-4 | no | "research solid state batteries for me" |
| `define_term` | open the dictionary, thesaurus, Wiktionary or Wikipedia page for a term | `open_url` | no | "look up serendipity in the dictionary" |
| `github_open` | open a repo, one of its sections, a profile or a search on GitHub; with a local folder, the repo it was cloned from | optional `git_status`, `open_url` | no | "open the issues for torvalds linux on github" |
| `clipboard_to_note` | append the clipboard, timestamped, to `~/Notes/clipboard.txt` | `get_clipboard`, `append_text` | no | "save what's on my clipboard to a note" |
| `clipboard_to_file` | save the clipboard as a new file; never overwrites | `get_clipboard`, `create_folder`, `write_new_file`, optional `open_with` | no | "save my clipboard as quote dot txt on the desktop" |
| `save_page_notes` | fetch a page and save its text as a new Markdown file in `~/Notes/pages` | optional `get_clipboard`, `fetch_url`, `create_folder`, `write_new_file` | no | "save the text of this page" |
| `read_later` | add a link (given, or on the clipboard) to `~/Notes/read-later.md` | optional `get_clipboard`, `append_text` | no | "save this page for later" |
| `code_scratchpad` | new throwaway file in one of 15 languages (starter, or the clipboard), opened in an editor; created, never run | optional `get_clipboard`, `create_folder`, `write_new_file`, `open_with` | no | "give me a scratch file for some javascript" |

How these sit next to the three older skills: `setup_project` makes an *empty* folder,
runs `git init` and opens VS Code (two prompts); the three `new_*` skills write *starter
files* for a named language and run nothing unless asked to. `quick_note` saves text the
user dictates; `clipboard_to_note` saves what they copied. `backup_folder` copies a folder;
`git_snapshot` commits one.

`write_new_file` (create a file only if it doesn't exist) is the files domain's tool. The
scaffolding, clipboard-to-file, page-saving and scratchpad skills reach it only through
`dispatch("write_new_file", ...)`, so they need it registered and nothing else.

Things worth knowing before changing these:

- **Shell strings.** The only ones are the `run_command` steps above. Paths are expanded
  with `Path.expanduser()` first and every interpolated value goes through `shlex.quote`
  (`dev_common.q`). The check commits with the message
  ``it's "quoted" $(touch X) `touch X` ; touch X`` and asserts `X` doesn't exist.
- **Never overwriting.** A scaffold stops before creating anything if the folder exists
  and isn't empty; every file goes through `write_new_file`. Project names are plain
  folder names (no `/`, no leading `.` or `-`).
- **Fetched text is untrusted.** `fetch_url` returns whatever the page's author wrote, and
  the model reads it. Its description says to treat it as information, not instructions,
  but the real boundary is the tiers: anything destructive a page talks the model into
  still stops at a confirmation. `fetch_url` itself can't reach this machine or the local
  network, so a page can't use it to read a local service.
- **`open_with` has two independent halves.** The app list is a denylist, so it's only as
  complete as the list. The path rules are what hold if it misses one: after resolving
  symlinks, only a non-bundle folder or a non-executable content file gets through, and
  none of those is something macOS itself runs. That's also why `code_scratchpad` has no
  shell language: `.sh` files are refused.

`agent/scripts/skills_check_dev.py` covers all of it without touching the machine: `HOME`
is a temp dir, the browser/app launches and the clipboard are recorders, `fetch_url` runs
against a local `http.server` (redirect chains and loops, a 5 MB body, a gzip bomb, binary
types, a stalled and a dripping response, refused schemes and hosts), and git runs in temp
repositories.

#### Routing and end-to-end checks (these call the real model)

The three domain checks above prove each capability works when called. They say nothing
about whether a spoken request reaches the right one, which is the part a user meets. Two
more scripts cover that. Both send the whole tool list on every request (91 capabilities,
about 22,700 input tokens), so both cost real money; neither is part of the routine suite.

```bash
cd agent
./.venv/bin/python scripts/skills_routing_check.py            # ask whatever isn't cached yet
./.venv/bin/python scripts/skills_routing_check.py --report   # table from the cache, free
./.venv/bin/python scripts/skills_routing_check.py --only pomodoro,quick_timer
./.venv/bin/python scripts/skills_routing_check.py --payload  # size of the tool list, free

./.venv/bin/python scripts/skills_e2e_check.py --scripted     # no model: sandbox + assertions
./.venv/bin/python scripts/skills_e2e_check.py                # real model, all scenarios
./.venv/bin/python scripts/skills_e2e_check.py timers git_commit
```

**`skills_routing_check.py`** holds two natural phrasings per skill (at least one that
avoids the skill's name), one per directly-askable tool, and a set of negatives where a
similar-sounding capability must not be chosen ("create a note called X" must not reach a
file-writing skill, BUGS.md #16). Each phrase is sent twice as a single first turn, built
exactly as `agent_loop.run()` builds it, and the tool calls are recorded and thrown away:
no tool function runs. A first turn that only looks (`file_info`, `get_clipboard`,
`git_status`, `repo_overview`...) is answered with a canned result and given one more turn,
so a look-before-acting is scored on what it goes on to do. Each case names the right
route, any alternative accepted and why, and what is forbidden; the report gives hit /
alt / UNSTABLE / MISS per phrase and per capability, then the confusions. Answers are
cached in `skills_routing_cache.json`, keyed by phrase, run and a hash of the whole prompt
surface, so editing any description makes the next run ask again (`--stale-ok` scores old
answers anyway, marked `*`). After changing a description, rerun that capability and its
neighbours with `--only`: the point is to show the confusion went away, not moved.

**`skills_e2e_check.py`** drives `agent_loop.run()` and the real `dispatch()`, so skills
run their real steps, and asserts on the effect: files in the sandbox, what the recorders
saw, and a reply that is non-empty, free of markdown and says what happened. Scenarios
span the three domains and include follow-ups that only work if the conversation carries
over ("zip that up too", "now open it in VS Code", "cancel the ten minute one") and three
refusals (a file that already exists, a folder that doesn't, `open_file` on a script)
where the reply must say it did not happen. Because a model chooses what gets called,
isolation is structural rather than trusted: `HOME` is a temp dir rebuilt per scenario and
the process chdirs into it; every tool function is wrapped so a path outside the sandbox
is refused and fails the scenario; a tool with no entry in the script's policy table is
refused; `run_command` never reaches a shell (only allowlisted `git` against a sandbox
repository runs, as an argv list; everything else is declined at the confirmation); GUI
tools are declined; launchers, banners, speech, media keys, volume, clipboard, Spotlight,
`fetch_url`, screenshots and the Trash are recorders or stand-ins; weather uses a canned
local server; timers are real with the clock moved forward. A sandbox self-test runs
before any model call. `--scripted` swaps the backend for one that replays each
scenario's written plan: it proves the sandbox and the assertions for free and is safe to
run any time, but says nothing about what the model would choose.

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

### Security model

Leutheria acts on your real Mac on the say-so of a language model, and the model reads text
other people wrote (web pages, files, your clipboard, repositories). So what it may do is
decided by fixed rules in the agent (`core/dispatcher.py`, `core/path_policy.py`), not by the
model. `scripts/security_policy_check.py` replays the attacks these rules were built against.

**Without asking**, it may: read ordinary files, list and search folders, and create, add to,
copy, move, zip, unzip and Trash files inside your user folders — Documents, Desktop,
Downloads, Pictures, Movies, Music, and Notes, Projects, Scratch and Snapshots in your home
folder (the places its skills save to). Nothing it does there overwrites: writes only create
or append, moves and copies refuse an existing destination, deletes go to the Trash.

**It asks first** (a card on screen, also spoken) before:
- running any shell command;
- writing, moving or trashing anything outside those folders — elsewhere in your home folder,
  on another disk, in a temporary folder — or trashing a whole user folder. The card says why;
- contacting or opening a web address after it has read a file, the clipboard, a web page,
  a repository or command output in the same request, unless you named that site yourself.
  The card shows the full address;
- controlling another app (once per app, for 15 minutes);
- anything you've marked "always ask" in the Library — and everything, if its settings file
  is damaged.

A spoken "yes" approves only what was read out in full. A shell command that is long, chains
or redirects (`|`, `;`, `&&`, `$( )`, `>`…), uses quoting, or calls a downloader, interpreter
or `rm`/`sudo` needs a click on the card; "no" always works by voice.

**It will never**, whatever you approve or set: change its own program, settings, approvals,
log or system prompt; write in `~/Library`, in hidden files and folders at the top of your home
folder (`.zshrc`, `.ssh`, `.gitconfig`, `.config`…), in system folders (`/System`, `/Library`,
`/usr`, `/etc`, `/Applications`…), inside an app or other package, or in a repository's `.git`
folder; write through a symbolic link; move something into a temporary folder or straight into
the Trash folder; read keys and credentials (`~/.ssh`, `~/.gnupg`, `~/.aws`, `.env` files,
`*.pem`, `id_rsa`…), anything in `~/Library` (passwords, cookies, browser profiles), or its own
API key. Text from files, pages and the clipboard never goes into its log, which keeps sizes,
paths and outcomes only and rotates at 5 MB.

**What this does not protect against**, honestly:
- Prompt injection can still make it *ask* for something you shouldn't approve. Read the card;
  the line at the end of a spoken prompt is the agent's, the sentence before it is the model's.
- It can still write junk into your own folders, and it can still read your ordinary
  documents and say what's in them, to you or (if you approve the address) a web site.
- Spotlight results and folder listings can show names of files it isn't allowed to read.
- Any program running as you on this Mac can already do everything Leutheria can; the agent
  is not a sandbox against local software. (The WebSocket requires the per-launch token.)
- A check-then-act race: a path that is swapped for a symlink between the check and the write.

### Confirmation flow

Any tool tagged `"destructive"` in `agent/tools/registry.py` (currently just `run_command`)
doesn't execute immediately. Instead, `dispatcher.dispatch()` sends a
`confirmation_required` message back over the same connection and **suspends** until a
matching `confirm` message arrives (or `CONFIRMATION_TIMEOUT` — 120s — elapses, which
counts as a decline):

```python
# 1. you send (manual dispatch, so LEUTHERIA_MANUAL_DISPATCH=1; a text command that
#    makes the model call run_command pauses the same way):
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

A confirmation nobody answers within `CONFIRMATION_TIMEOUT` is declined, and the agent
then sends `{"type": "confirmation_resolved", "id": "...", "approved": false, "by":
"timeout"}` on the connection that was asked (mirrored to overlays), so every card
settles on that one event rather than staying live until the command's reply arrives.
It is logged as `confirmation_timed_out`.

### Where tools run, and `run_command`'s limits

The agent is one asyncio loop: it reads every client's messages, answers control
messages, resolves confirmations and drives the voice session. A tool that blocked it
blocked all of those, so `dispatcher._run_tool` does not call tool functions on it:

- **By default a tool function runs on a worker thread** (one daemon thread per call —
  nothing queues behind a slow tool, and nothing holds the agent open at exit). While
  `fetch_url`, a 120 s `zip_folder` or a long `run_command` is working, the loop keeps
  answering: a confirmation for a different call can be asked and answered, and the
  Library still loads.
- **A tool whose registry entry says `"thread": "main"` runs inline on the loop
  thread**, because it does not work anywhere else. Each flagged entry carries the reason
  in a comment. They are `focus_app`, `describe_ui`, `type_text`, `press_keys` (they find
  the target app through NSWorkspace, whose state only refreshes when the *main* thread's
  run loop is pumped — measured: an app launched after startup was never seen from a
  worker thread, so off-thread the focus re-check that guards typing would silently
  compare against a frozen answer), `media_control` (AppKit events; instant), and
  `set_timer` / `list_timers` / `cancel_timer` (asyncio tasks). A `"main"` tool holds the
  loop for as long as it runs, so the flag is for tools that are quick, not a default.
- An `async def` tool function is awaited on the loop.
- **Any exception a tool raises becomes `{"ok": false, "error": ...}`** and is logged as
  `tool_error` with its traceback; it never takes the request or a skill down.
- The file tools whose safety is "check, then write" (`move_file`, `copy_file`,
  `write_new_file`, `append_text`, `create_folder`, `take_screenshot`, `trash_file`,
  `zip_folder`, `extract_archive`) share one lock, so no two of them ever run at once —
  what running inline used to guarantee for free.
- Preferences, trust, grants, session state and timers are still only touched on the loop
  thread (in `dispatch()` itself and the `"main"` tools). The event log is the one shared
  thing a worker may write, and `log_event` takes a lock.

`run_command` is bounded, since it is the one tool that otherwise isn't:

| limit | value | why |
|---|---|---|
| time | 300 s by default; `timeout_s` per call, clamped to 1–1800 s | Long enough for a clone, an install or a test run. The limit is there for the command that never ends (a dev server, something waiting for input), and the requested value is in the args the user approves. |
| on timeout | SIGTERM to the command's whole process group, SIGKILL 2 s later | The command gets its own session, so "everything it started" is one group; killing only the shell would orphan the real work. The result has `"timed_out": true` and an `error` saying so, plus whatever was printed. |
| stdin | `/dev/null`, no terminal | A command that stops to ask a question gets end-of-file and fails at once. |
| output | first 8,000 + last 12,000 bytes of each stream | The rest is counted and dropped (`"truncated": true`); the start says what ran, the end says how it failed. |
| shutdown | commands in flight are killed when the agent gets SIGTERM or Ctrl-C | They no longer share the agent's process group, so nothing else would. A command the user deliberately backgrounded (`server &`) and that already returned is left alone. |

```bash
cd agent && ./.venv/bin/python scripts/dispatch_loop_check.py
```

proves all of this through the real `dispatch()`: a ticker keeps ticking and a second
call's confirmation is answered while a slow tool runs; a `"main"` tool runs on the loop
thread; exceptions and cancellation; the writer lock; `run_command`'s timeout, kill, cap
and shutdown; the confirmation timeout message; and the fixed older tools
(`take_screenshot` never overwrites, `create_folder` on a file, `setup_project`'s
quoting). It opens no port, window or microphone and captures no screen.

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
doesn't need a key at all, since it skips the LLM entirely — but it is off unless
`LEUTHERIA_MANUAL_DISPATCH=1`, see "Manual tool dispatch").

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
question that's already been answered. `"by": "overlay"` means an overlay client answered
it (below), and `"by": "timeout"` (always `approved: false`) that nobody answered within
`CONFIRMATION_TIMEOUT`.

### Overlay clients (`hello` and roles)

(An overlay authenticates like any other client — the handshake rules under
"Authentication" come first; `hello` only says what an already-accepted connection is.)

A client may send one optional message first:

```python
{"type": "hello", "role": "overlay"}
```

That makes it an **overlay**: a second *view* of the voice session (the native Qt overlay),
not a place a conversation happens. A client that never sends `hello` (the Electron app,
a debugging script), or says hello with any other role, behaves exactly as before.

- **Never the primary.** Voice-session commands run on the newest *non-overlay*
  connection, so replies, confirmations and skill proposals keep going to the Electron
  window however the connections were ordered. The only gap is the moment between an
  overlay's handshake and its `hello`, which on loopback is well under a millisecond.
- **What an overlay receives:** every broadcast (`voice_state`, `session_started`,
  `session_ended`, `audio_level`, and the current `voice_state` on connect), plus a copy
  of these messages when they go to the primary:
  - `confirmation_required`, for every confirmation (the overlay decides whether to
    show it; the Electron overlay only shows one while a session is live);
  - `confirmation_resolved` (`by`: `"voice"`, `"ui"` when the Electron app answered, or
    `"timeout"` when nobody did);
  - `speaking` started and ended, which carries the text of a spoken confirmation prompt;
  - `transcript` and `response` for the voice session only (`"origin": "voice_session"`).
    Typed commands, push-to-talk, tool results and skill proposals are not copied.

  With no UI connected at all, voice-session transcripts and responses still reach
  overlays. A confirmation in that state is turned down automatically, as before, so no card
  ever appears.
- **Confirming from an overlay:** send the ordinary
  `{"type": "confirm", "id": "...", "approved": true}` (`remember` is passed through if
  present). The overlay has no confirmations of its own, so the id is looked up among
  the UI connections' pending confirmations. When it resolves one, **every** client gets
  `{"type": "confirmation_resolved", "id": "...", "approved": true, "by": "overlay"}`
  once, so both cards settle. An unknown or already-answered id is ignored.
- **When the UI answers:** a `confirm` from a UI connection now also sends
  `confirmation_resolved` with `"by": "ui"`, but only to overlays. The UI settles its own
  cards when you click, and it still isn't sent one.
- **What an overlay may send:** `hello`, `confirm`, and the control messages
  (`voice_session`, `inventory`, …). Anything else (a command, audio, a `tool` dispatch,
  `skill_response`) gets `{"type": "error", "text": "overlay connections can only send
  hello, confirm and control messages"}` and is not run.

For every client, a frame that parses as JSON but isn't an object (`5`, `[]`) gets
`{"type": "error", "text": "expected a JSON object"}`. It used to close the connection
(BUGS.md #28).

`agent/scripts/overlay_protocol_check.py` checks all of this against the real server on a
side port (`LEUTHERIA_VOICE_SOURCE=<wav> ./.venv/bin/python scripts/overlay_protocol_check.py`).

## Logs

Every message received, every LLM decision, and every tool call (regardless of whether
it came from the LLM loop or a direct `{"tool": ...}` message) is appended as one JSON
line to `agent/logs/events.jsonl` (gitignored). Event types: `message_in`, `message_out`,
`ws_auth` (startup: where the socket's token came from — never the token), `ws_rejected`
(a refused handshake, by reason, rate-limited), `manual_dispatch_refused`,
`llm_turn` (what Claude decided that turn), `tool_call` (which tool ran, with what args
and result), `stt_transcript` (what Whisper heard), `tts_error` (a synthesis failure --
non-fatal; `backend` says which engine failed, and `fallback: "piper"` means the reply was
still spoken by Piper), `confirmation_requested`/`tool_declined` (the destructive-tool
confirmation flow), `confirmation_answered_by_voice`, `confirmation_timed_out`,
`tool_error` (a tool function raised; the traceback is in the record),
`shutdown_killed_commands`, `preference_changed`/
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

**The app says `error` and the log shows `Unexpected server response: 401`**

The agent refused the app's token. When the app started the agent itself this means
something else answered on the port — usually an agent left over from another run (or a
standalone one), which has a different token; stop it (`pkill -if "python -m agent"`) and
start the app again. With `LEUTHERIA_NO_SPAWN=1` it means `LEUTHERIA_TOKEN` in the app's
environment isn't the one that agent was started with (see "Authentication"). A `403`
means the client sent an `Origin` header, which no supported client does. The agent's side
of it is a `ws_rejected` line in `agent/logs/events.jsonl`.

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
