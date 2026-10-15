# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Leutheria: a macOS voice-controlled assistant. An **Electron shell** (`app/`) spawns a
**Python sidecar** (`agent/`) and talks to it over `ws://127.0.0.1:8765` with JSON messages.
All agent work — wake word (openWakeWord), STT (Whisper), TTS (ElevenLabs by default,
macOS `say` as the local fallback, Piper optional), LLM tool-calling (Anthropic `claude-opus-5`), tool/skill
execution — happens Python-side; Electron is UI + process supervision only.
`overlay-qt/` is a third piece: a native C++/Qt Quick build of the activation overlay, a
second client of the same WebSocket (off by default; `LEUTHERIA_OVERLAY=qt`). It has its
own README.

The Python server is a single asyncio loop. Anything blocking — LLM calls, Whisper, Piper,
skill-learning generation — goes through `loop.run_in_executor`, or it stalls the read loop
(including confirmations and management messages). Tool functions are covered by the
dispatcher, not by each caller: `dispatcher._run_tool` runs every synchronous tool on a
worker thread (a daemon thread per call) unless its registry entry says `"thread": "main"`,
which runs it inline on the loop thread. Three things must stay on the loop thread, and
each flagged entry says which applies: anything that reads NSWorkspace through
`ui_access` (the GUI tools — its state only refreshes when the *main* thread's run loop is
pumped, so off-thread the focus re-check goes stale, silently); anything that creates or
cancels asyncio tasks (the timer tools); and anything that touches the loop-thread
singletons (`session`, `trust`, `preferences`, `timers` — they have no locks, on purpose).
A `"main"` tool blocks the loop while it runs, so a new tool gets the flag only for one of
those reasons and only if it is quick. A worker-thread tool may call `log_event` (locked)
and nothing else shared. Skills are `async` and run on the loop; they must not block.
The model is reached only through
`core/llm.py:LLMBackend` (`llm_anthropic.py` is the one implementation); callers depend on
the interface, and multi-turn tool loops live in `agent_loop.py`, not the backend.
Every call carries all ~91 tool and skill definitions (~21k tokens), so the backend puts a
prompt-cache breakpoint on the last tool; without a cache hit a call costs about $0.11 of
input. Keep the tool list's order deterministic, never add per-request text to a tool
definition, and run `llm_cache_check.py` after changing how the prompt is assembled — a
cache miss raises no error, it just costs ten times as much.

`README.md` is the detailed reference (full wire protocol per message type, tool table,
troubleshooting). Read the relevant section there before changing protocol or agent internals.
`plans/` (gitignored) holds ROADMAP.md, NOTES.md (open questions), and BUGS.md (numbered
bugs referenced from code comments and the README).

**Every real bug you find and fix gets an entry in `plans/BUGS.md`** — not just the ones
the user reports, and not only at the end of a task. Follow the existing format: When /
Symptom / Root cause / Fix / Verified / Lesson, numbered, newest last. Include the wrong
turns worth remembering (a fix that didn't work, a false all-clear, a misdiagnosis), since
avoiding a repeat investigation is half the file's value. A behaviour you decide is *not*
a bug after investigating goes in the section at the bottom. Anything the fix surfaces
that needs a decision later goes to `NOTES.md` instead.

## Commands

```bash
# Full app (spawns the Python agent itself)
cd app && npm start
# If ELECTRON_RUN_AS_NODE is set in the shell, Electron silently runs as plain Node:
cd app && env -u ELECTRON_RUN_AS_NODE npm start

# Python agent standalone (prints AGENT_READY, no Electron)
cd agent && ./.venv/bin/python -m agent

# Install
cd agent && /opt/homebrew/bin/python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cd app && npm install
brew install ffmpeg    # Whisper decodes audio through it

# Observe / stop
tail -f agent/logs/events.jsonl
pkill -if "python -m agent"; pkill -f "Electron.app"

# Checks (run from agent/; none of them open the microphone)
./.venv/bin/python scripts/voice_sim.py                 # voice session state machine, all scenarios
./.venv/bin/python scripts/voice_sim.py dismiss confirm # just the named scenarios (--list shows them)
./.venv/bin/python scripts/skill_format_check.py        # generated-skill validation
./.venv/bin/python scripts/tts_check.py                 # TTS backends + fallbacks, fake ElevenLabs + stubbed say
./.venv/bin/python scripts/licenses_report.py --write   # regenerate THIRD_PARTY_LICENSES.md's package tables
./.venv/bin/python scripts/overlay_protocol_check.py    # overlay role, real server on a spare port
./.venv/bin/python scripts/ws_auth_check.py             # socket authentication, real server on a spare port
./.venv/bin/python scripts/skills_check_system.py       # system tools + skills; volume/media/banners/speech stubbed
./.venv/bin/python scripts/security_policy_check.py     # path zones, taint, voice approval, fail-closed, log redaction
./.venv/bin/python scripts/security_tools_check.py      # safe tier launches nothing: openers, archives, repos, fetch deadline
./.venv/bin/python scripts/dispatch_loop_check.py       # tools off the loop, run_command limits, confirmation timeout
./.venv/bin/python scripts/panels_check.py              # show_panel/hide_panel messages, list_timers control
./.venv/bin/python scripts/voice_server_check.py        # real server + real LLM; speaks aloud, needs API key
./.venv/bin/python scripts/skills_e2e_check.py --scripted   # agent_loop + dispatch in a sandbox home, written plans, no model
./.venv/bin/python scripts/llm_cache_check.py          # REAL MODEL, ~$0.15: is the tool list being read from the prompt cache?
./.venv/bin/python scripts/skills_routing_check.py --report # routing table from the committed cache, no model
./.venv/bin/python scripts/export_tool_catalog.py --check  # is agent/tool_catalog.json current? (no flag: re-export)
./.venv/bin/python scripts/registry_pipeline_check.py   # skill registry: catalog, submission checks, index sign/verify, yank

# These two call the REAL MODEL and cost money: every request carries the whole tool list (~22,700 input tokens)
./.venv/bin/python scripts/skills_routing_check.py      # phrase -> which capability; nothing dispatched; answers cached
./.venv/bin/python scripts/skills_e2e_check.py          # 18 requests end to end in the sandbox home

# Qt overlay (needs `brew install qt ninja`)
cmake -S overlay-qt -B overlay-qt/build -G Ninja -DCMAKE_PREFIX_PATH=/opt/homebrew/opt/qt
cmake --build overlay-qt/build
ctest --test-dir overlay-qt/build                       # all three Qt Test suites
ctest --test-dir overlay-qt/build -R overlaymodel       # one suite
overlay-qt/build/tests/tst_overlaymodel endLingersThenHides   # one test function

# Electron-side token plumbing + the mock's enforcement (no Electron, no Python)
cd app && node scripts/auth-check.js

# UI work without Python: a scripted agent on port 8799
cd app && node scripts/mock-agent.js
# UI regression pass: own mock + own Electron, every view/state, both themes, 1180 and 940 px
cd app && node scripts/ui-capture.js --out <dir>     # then LOOK at the PNGs
cd app && env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
```

Requires Homebrew Python 3.12 (system 3.9 is too old). Always invoke the venv binary
(`./.venv/bin/python`) directly — there is no activate-then-run convention here.
There is no linter and no unit-test framework on the Python/JS side: the `scripts/` checks
above are the regression suite, and UI changes are verified by running the app (against the
mock agent or a real one) and looking at screenshots. Add a scenario to the relevant script
when fixing a bug it could have caught.

Testing without disturbing a running app — `npm start` pkills any running agent, and port
8765 may be in use — goes through env overrides: `LEUTHERIA_PORT` (agent listens elsewhere),
`LEUTHERIA_VOICE_SOURCE=<wav>` (a WAV replaces the mic, so no permission prompt and no live
audio), `LEUTHERIA_AGENT_URL` + `LEUTHERIA_NO_SPAWN=1` (Electron connects to an agent it
didn't start; it then needs that agent's token in `LEUTHERIA_TOKEN`, or reads a standalone
agent's token file), `LEUTHERIA_CAPTURE_DIR` (PNG of each window on every voice-state change).

`ANTHROPIC_API_KEY` (and the optional `ELEVENLABS_API_KEY`) live in `.env` at the **repo
root**, not in `agent/` — `__main__.py` calls `load_dotenv()`, which walks up from the
process cwd. A script run from elsewhere must pass the path explicitly. Keys are read once
at startup.

## Architecture invariants

**Tools and skills are indistinguishable to the model.** Both are entries in the `tools`
list sent to Claude. `core/dispatcher.py` is the single place that knows the difference:
it resolves a name against `skills/registry.py:SKILLS` first, then `tools/registry.py:TOOLS`.

**A skill calls `dispatch()` for each of its own steps.** That's why a skill can't be used
to smuggle past the confirmation gate — a destructive sub-step pauses exactly as a directly
called destructive tool would. Preserve this when writing or editing skills; never call a
tool function directly from a skill.

**Generated skills are data, never code.** Approved proposals are written as JSON step lists
to `agent/agent/skills/generated/*.json` (gitignored) and executed by the one reviewed
interpreter, `skills/template_skill.py:run_template()`. Skill learning must never emit Python.

**Generated skills call tools only, and are validated on every way in.** A generated step's
`tool` must be a registered tool, never a skill (hand-written or generated): every learned
skill stays a flat, auditable step list with no recursion and nothing hidden behind another
skill's name. `skills/validate.py` is the format contract, written to hold for a file nobody
on this machine wrote (a future shared skill): it runs at proposal, at `register_skill()`, and
at load, where a bad file is logged as `skill_load_rejected` and skipped, never fatal. A
generated skill's name can't be a tool's or built-in skill's (dispatch resolves skills first,
so it would shadow it — BUGS.md #23), and its file path comes only from the validated name.
`content_hash` covers steps + the behavioural part of `input_schema`, deliberately not prose;
changing what it covers means bumping `HASH_DOMAIN`. Placeholders are `{param}` with `{{`/`}}`
escapes, substituted in one pass — never go back to `str.format` (BUGS.md #24).

**GUI control is the one thing that isn't safe by construction.** The `scoped` tier
(`focus_app`, `type_text`, `press_keys`) is gated per *target app* for a time-boxed
window rather than per call, because nobody approves 200 keystrokes. `core/ui_access.py`
holds the two structural mitigations — `DENIED_APPS` (never drivable or readable, grant
or no grant; Terminal is on it because typing into a shell would bypass the whole
confirmation model) and secure-text-field redaction. `core/session.py` holds the grants;
they are in-memory only and die with the process, unlike `trust.py`'s "always" scope.
`type_text`/`press_keys` re-check focus before every chunk and abort mid-stream if it
moves — the premise of a long session is that the user keeps using their machine, so
that's a live TOCTOU hole, not an edge case.

**Safety tiers are structural, not advisory.** There are three tiers: `safe`, `scoped` (GUI
control, above), and `destructive`. `run_command` is the only `"destructive"` tool
because it's the only unbounded one; the rest of the non-GUI tools are `"safe"` *by construction* —
`trash_file` uses the recoverable Trash, `move_file`/`copy_file` refuse to overwrite,
`append_text` can't truncate. A new tool that can destroy data must either be redesigned to
be structurally safe or tagged `"destructive"`.

**"Safe" also means *where*, and that is decided in `dispatch()`, never in the tool.** Every
path argument is declared in the tool's registry entry (`"paths": {"source": "remove",
"destination": "move_into"}`; modes read / inspect / archive / copy_from / write / remove /
move_into) and judged by `core/path_policy.py` on the realpath and file identity (case- and
symlink-proof, never string prefixes) before the tool runs. A schema argument that looks like
a path but isn't declared makes the tool refuse — a new tool can't forget. Write-side zones:
FREE (no prompt) in `FREE_HOME_FOLDERS` — Documents, Desktop, Downloads, Pictures, Movies,
Music, and the skills' defaults Notes, Projects, Scratch, Snapshots; ASK (a confirmation with
a system-written reason line) anywhere else, including /Volumes and temp folders; DENY (no
prompt, trust or preference reaches past it) for the agent's install tree and state files
wherever they're configured, `~/Library`, any top-level dot entry in home, system roots,
anything inside a bundle or a `.git`, temp folders and `.Trash` as a move target, and a
symlinked destination. Reads are denied for credentials (`~/.ssh`, `~/.gnupg`, `~/.aws`,
`.env*`, `*.pem`, `id_*`, `.netrc`…), `~/Library`, and the agent's own `.env`/state/log.
Roots come from the effective home, so checks point HOME at a temp dir; a check's scratch
folder outside it goes in `path_policy.EXTRA_FREE_ROOTS`, never anywhere else.

**Taint is per request and declared, too.** `"untrusted_output": True` marks a tool whose
result is text a stranger may have written (files, clipboard, pages, repos, command output,
on-screen text); `"outbound": "<arg>"` marks a tool that contacts or opens that URL. Once a
request (a `session.begin_request` ContextVar opened by `agent_loop.run()`, so skills' steps
share it) has run an untrusted tool, an outbound call asks — full URL on the card — unless
the URL or its host is in the user's own words. Never hard-code tool names for this.

**Settings fail closed.** An unreadable `preferences.json` makes every capability ask
until it's fixed and the agent restarts; an unreadable trust file trusts nothing; neither is
overwritten in place (kept as `.corrupt-<time>`); the inventory carries `settings_errors`.

**A spoken confirmation always ends with the agent's own line** naming the real action
(`dispatcher._system_line`: the exact command, up to 200 characters, or tool + key argument),
after the model's sentence. A spoken "yes" is refused for a command
`voice_confirm.needs_click()` flags (too long to have been read out, shell metacharacters,
quoting, non-ASCII, downloaders/interpreters/deleters) — the card stays open for a click. A
spoken "no" always counts. Timer reminders are spoken after a fixed "Timer reminder:" lead-in.

**The event log never holds content.** `log_event` scrubs values under content keys
(`logging_util.CONTENT_KEYS`) to their size and caps every other string; it rotates at 5 MB.
Read the log only through `logging_util.iter_records()` (spans rotated files). Where that safety is "check, then write",
the tool's name also goes in `dispatcher._FS_WRITERS`: tools run on worker threads now, and
that set shares one lock so two of them can never interleave. `run_command` is bounded even
so (timeout with a process-group kill, capped output — README "Where tools run"), and every
tool's subprocess call takes a timeout.

**Session state is a module-level singleton** (`core/session.py`: focus, rolling history,
grants). Threading it through `dispatch()` would change every skill's
`run(args, websocket, pending)` signature for no gain on a single-user local agent.
`agent_loop` carries only plain user/assistant text across turns — replaying a stale tool
trace invites the model to treat an old result as still true. What *did* run is listed
separately in the system prompt's "Current session" note (`session.record_actions`: tool,
args, succeeded/failed, never result payloads). Without it the model saw its own "Ran it"
with no evidence behind it, disowned it, and re-ran the command (BUGS.md #21).

**Preferences gate twice.** `core/preferences.py` overrides (`enabled`, `requires_confirmation`)
are applied both when building the schemas sent to Claude *and* independently in
`dispatch()` — so a disabled capability is neither describable nor runnable, even if baked
into a stale plan or a learned skill's step.

**Only clean successes become skills.** `skill_learning.is_clean_success()` rejects any
trace containing a failed step: a skill replays its whole step list, so a sequence with a
failure isn't a procedure, it's a procedure that didn't work plus the recovery attempts.

**`agent/logs/events.jsonl` is the single source of truth** for the Library's usage counts,
the Activity history, and skill-learning's repeat detection. Anything that should show up in
the UI must be logged via `core/logging_util.py`. Skill learning checks the log for a past
matching run *before* the current request logs its own entry (checking after self-matches —
BUGS.md #6).

**Management messages never touch the LLM.** `inventory` / `set_preference` / `delete_skill` /
`revoke_trust` / `revoke_grant` and the voice ones (`voice_session`, `get_voice_settings`,
`set_voice_settings`, `list_input_devices`, `list_wake_models`) are answered inline in the
read loop by `core/control.py`, matched by `request_id`, so UI reads can't block or be
blocked by a running command. The one exception is `list_input_devices` with `refresh: true`:
it has to close and reopen the mic, so `server.py` answers it from a background task (same
`request_id`).

**The socket is authenticated, and refuses browsers.** Loopback is not a boundary: every
local process and every web page can reach 127.0.0.1. `core/ws_auth.py` checks each
connection in the opening handshake, before the upgrade: any request with an `Origin`
header is refused (403 -- no value is allowed, not `null`, not `file://`; no real client
sends one), and it must carry `Authorization: Bearer <token>` (401, constant-time compare).
The token is per launch: Electron generates it (`app/agent-auth.js`) and passes it to the
sidecar and the Qt overlay in their **environment** (`LEUTHERIA_TOKEN`) -- never argv, a
file, a log line or a renderer; a standalone agent generates its own into a 0600 file under
`~/Library/Application Support/Leutheria/`. The agent pops the variable from `os.environ`
once read, so `run_command` children don't inherit it. `server.serve()` is the only place
`handler` is bound and it always attaches the check -- never call `websockets.serve` with
`handler` anywhere else, never add an unauthenticated mode or a "dev" bypass (a missing
token means generate one, an unusable one means refuse to start), and never widen the bind
address. The bare `{"tool": ...}` manual dispatch is off unless `LEUTHERIA_MANUAL_DISPATCH=1`
(check scripts set it); no real client sends it. A new client of the socket needs the
header; a new check script that starts a server sets its own random `LEUTHERIA_TOKEN`.

**The system prompt applies to conversation only.** `agent/SYSTEM_PROMPT.md` is loaded once at
import in `agent_loop.py` and passed as `system` on conversational calls. `skill_learning.py`
passes no system prompt — its calls must return strict JSON, and persona/tone instructions
would corrupt structured output. Edits to the file take effect on the next agent restart.

**The sidecar owns the mic and the speaker.** `core/voice_session.py` captures audio and
`tts.speak()` plays replies through sounddevice; no audio crosses the WebSocket (the old
base64 `speech` message is gone). This is load-bearing, not incidental: the session is
half-duplex — captured frames are discarded while `speak()` is playing plus a 300 ms tail —
and only the process driving the speaker knows exactly when that is. Route any new playback
through `speak()` or the mic will hear it and act on it. Voice commands run on the primary
connection (`server._primary()`: the newest one that isn't an overlay); every spoken input,
push-to-talk or session, goes through `server.handle_transcript()`. The session's frame source is injectable, which
is how `scripts/voice_sim.py` tests it without a mic — keep it that way.

**Overlay clients are never primary.** A client that opens with `{"type":"hello","role":
"overlay"}` (the Qt overlay) gets voice broadcasts plus a mirror of what the primary is
sent that an overlay needs to draw (confirmations, `speaking`, voice-session transcript and
reply), and may send only `hello`, `confirm` and control messages; its `confirm` resolves
the primary's pending confirmation. Mirroring is done by wrapping the primary's socket
(`server._MirroringSocket`), precisely so nothing new is threaded through `dispatch()` or
skill signatures. A client that never says hello behaves as it always did. Two overlays
exist (Electron's `app/renderer/overlay/`, Qt's `overlay-qt/`) with the same state rules
and the same asset `manifest.json`; change a rule in one and change it in the other.

**TTS engines sit behind `core/tts_backends.py:TTSBackend`**, mirroring `LLMBackend`. The
selected backend and voice are read from voice settings per utterance; any failure of the
selected one (no key, network, HTTP error, timeout, Piper not installed, unknown voice)
falls back to macOS `say` in the system voice for that utterance and logs `tts_error` — a
reply is never silent because of the chosen voice. `say` only ever renders to a temp file
(`-o`, fixed argv, text on stdin); playback stays in `speak()`. **Piper is optional because
it is GPL-3.0**: it lives in `requirements-piper.txt`, is imported only inside
`PiperBackend` (never at module level — a build without it must start), and the Voice view
lists it only when `tts_available.piper` is true. Don't add a GPL dependency to
`requirements.txt` or `app/package.json`; `THIRD_PARTY_LICENSES.md` is the inventory
(regenerate its tables with `agent/scripts/licenses_report.py`). API keys are
environment-only and never written to `voice_settings.json`.

**Timers are the one thing that acts with no request in flight.** `core/timers.py` runs each
as an asyncio task and, when it ends, goes through the same two doors as everything else:
`dispatch("show_notification")` for the banner (so the Library's switches apply) and
`tts.speak()` for the line — never any other playback, or the mic hears it. It finds the
primary connection itself at fire time (`server._primary()`, looked up via `sys.modules`
so nothing imports the server), which is what keeps timers out of `dispatch()` and skill
signatures. A timer can only notify and speak; don't give one the ability to run a tool
chosen at set time — that would be a deferred action nobody is present to confirm. Timers
are in-memory only, and count wall-clock time (macOS's monotonic clock stops during sleep).

**Nothing in the safe tier can launch code.** `open <x>` runs whatever `x` is, so no safe
tool hands `open` anything it chose freely. `open_file` is an allowlist: content types (by
UTI) that don't act when opened, no execute bit or binary header, whose handler isn't a
terminal or launcher, named explicitly. `open_with` uses the same classifier
(`open_file.classify`, one place to get right) plus allowances for source and undeclared
text files into a named non-runner app. `open_url` takes only `http`/`https` URLs with a host
(never a path, `file:` or a custom scheme). `open_app` takes a name, resolved to a bundle in
the Applications folders, and passes that bundle and nothing else. `extract_archive` never
creates an executable file and copies the archive's quarantine mark onto everything it
writes. `git_status` neutralises every program a repository's config can name. Widen any of
them by type, with a check in `scripts/security_tools_check.py`.

**Panels never take focus, and only the accent is user-coloured.** The floating panels
(`app/panels.js`, pages in `app/renderer/panels/`) are one NSPanel `BrowserWindow` each,
shown with `showInactive()` whether a `panel` message, the hot edge or a button opened
them; nothing calls `focus()` on one, and the chat panel's composer has no autofocus.
Keyboard focus arrives only when the user clicks in. A panel loads the main window's own
view module (`chat.js`, `library.js`, ...) inside a shell that stands in for `app.js`,
so fix a view once, not per window. Layout, hot edge, alerts anchors and appearance are
one file, Electron `userData/overlay-layout.json`, owned by `main.js` and validated by
`app/overlay-settings.js` on load and on every patch (bad field: default plus a log line;
unparseable: moved aside), never trusted raw. Appearance reaches every window through
`renderer/js/appearance.js`. The user can replace the brand violet (the accent, adjusted
for contrast) but not the state colours: cyan/amber/red/green mean live/waiting/failed/
succeeded, and an accent that reads as one of them is refused. The hot-edge test hook
(`LEUTHERIA_TEST_HOOKS=1`) exists only under that variable; keep it out of the normal path.

**The tool catalog is what the public skill registry knows about tools.** `agent/tool_catalog.json`
(committed) is `tools/registry.py` reduced to name, safety, input_schema, paths, outbound and
untrusted_output, plus the built-in skill names and a `catalog_version` digest; the registry's CI
(`skill-registry/`, the future `leutheria-skills` repo) validates submissions against it, at the
Leutheria commit pinned in `skill-registry/LEUTHERIA_REF`, by importing `skills/validate.py` on
Linux. So: whenever a tool is added, removed or its schema/paths/safety/outbound/untrusted_output
change, re-run `scripts/export_tool_catalog.py` and commit the result (`--check` fails until you
do), and keep `validate.py` (and `template_skill.py`, which it imports) stdlib-only.

TTS, skill learning and the voice session are additive: a failure in any of them is logged
and skipped (voice goes to `off`), never blocking or delaying the text reply.

## Conventions

- One flat `.py` per tool in `agent/agent/tools/` (no per-tool packages — that was tried and
  reverted), plus an entry in `tools/registry.py` with `fn`, `safety`, `description`,
  `input_schema`. Descriptions are prompt surface: write them for the model.
- Hand-written skills get their own module with `run(args, websocket, pending)` and a
  `SKILLS` entry; they are code, so the UI can disable but not delete them.
- Spoken confirmations go through `core/voice_confirm.py:interpret_yes_no()`, which uses
  word-boundary regexes ("note" must not match "no"). An ambiguous or empty reply returns
  `None`, meaning "not an answer": handle it as a normal command, never as a guess. (The
  live voice session is the one exception: there a non-answer is shown but not run, since a
  second command must not start while the first is paused on a question.)
- Trust is keyed on the exact `(tool, args)` pair, never the tool name alone (`core/trust.py`).
- Renderer is plain classic scripts loaded in order from `index.html`, sharing one `LX`
  namespace — no framework, no build step, and not ES modules (module scripts don't load over
  `file://`). `preload.js` is the only bridge, for the overlay window too; pages have no Node
  access.
- A message type `main.js` doesn't recognise falls through to the `pendingIsChat` branch and
  can be shown in place of a real reply. Any new agent-to-UI message needs an explicit handler
  there, and a UI flow isn't verified until it's verified through `main.js` (BUGS.md #17).
- UI design system: every token (colour, type, space, geometry, motion) lives in one place,
  the first two blocks of `app/renderer/styles.css`, and `app/renderer/DESIGN.md` documents
  them and the patterns built on them -- extend it rather than improvising, and update it
  when you add a pattern. Colour means state, never decoration. No emoji in UI strings
  (icons are `PATHS` in `js/util.js`). The two overlays share the design: a visual change
  to the Electron overlay is a change to the Qt one too.
- Animated overlay assets are **animated WebP** (decided 2026-10-08): the one format with
  real transparency that plays in both overlays. Homebrew's Qt has only the AVFoundation
  media backend, which can't decode WebM, so a transparent WebM works in Electron and falls
  back to the placeholder orb in Qt. Recipe in `app/renderer/overlay/assets/README.md`.
- Expand `~` with `Path(...).expanduser()` before building any shell string — `/bin/sh`'s `cd`
  won't expand it inside quotes (this caused a real silent `git init` failure).
- Three macOS behaviours in `ui_access.py`, all measured rather than assumed, all of which
  fail silently rather than raising. Don't "simplify" any of them away:
  - **All** NSWorkspace state — `frontmostApplication()` *and* `runningApplications()` — is
    notification-driven and never updates in a process with no run loop, so it stays frozen
    at process start forever. Every read goes through `_pump()` first. This bit twice: the
    second time, an app launched after the agent booted was permanently invisible, so the
    agent insisted a visibly-open app wasn't running.
  - The AX system-wide focused-app query returns -25204 for Chromium/Electron apps, so it's
    the fallback, not the primary.
  - A fresh `CGEvent` inherits live modifier flags, so typing after a shortcut is delivered
    as ⌘-shortcuts unless `CGEventSetFlags(event, 0)` is called.
- Escaping in a tool `description` is prompt surface: a stray `\\n` there told the model to
  send a literal backslash-n and it dutifully typed one.
- Replies are spoken aloud, so agent-facing prose (SYSTEM_PROMPT.md, tool descriptions the
  model echoes) should avoid markdown — TTS reads the punctuation literally.
- `main.js` forwards transcript events to the main window *and* chat panels (`sendToUi`),
  numbered (`_seq`) so a panel opened mid-session replays the session without duplicates.
  A new transcript event goes through `record()` there, or a chat panel never sees it.
- Local state stays out of git: `preferences.json`, `trusted_commands.json`, `voice_settings.json`,
  `home_location.json`, `logs/`,
  `skills/generated/`, `assets/voices/`, `.env`, `plans/`.
