# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Leutheria: a macOS voice-controlled assistant. An **Electron shell** (`app/`) spawns a
**Python sidecar** (`agent/`) and talks to it over `ws://127.0.0.1:8765` with JSON messages.
All agent work — STT (Whisper), TTS (Piper), LLM tool-calling (Anthropic `claude-opus-5`),
tool/skill execution — happens Python-side; Electron is UI + process supervision only.

The Python server is a single asyncio loop. Anything blocking — LLM calls, Whisper, Piper,
skill-learning generation — goes through `loop.run_in_executor`, or it stalls the read loop
(including confirmations and management messages). The model is reached only through
`core/llm.py:LLMBackend` (`llm_anthropic.py` is the one implementation); callers depend on
the interface, and multi-turn tool loops live in `agent_loop.py`, not the backend.

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
```

Requires Homebrew Python 3.12 (system 3.9 is too old). Always invoke the venv binary
(`./.venv/bin/python`) directly — there is no activate-then-run convention here.
There is no test suite and no linter; verification is manual, via the app or a standalone
WebSocket client (see README "Running the Python agent standalone").

`ANTHROPIC_API_KEY` lives in `.env` at the **repo root**, not in `agent/` — `__main__.py`
calls `load_dotenv()`, which walks up from the process cwd.

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
`set_voice_settings`) are answered inline in the read loop by `core/control.py`, matched by
`request_id`, so UI reads can't block or be blocked by a running command.

**The system prompt applies to conversation only.** `agent/SYSTEM_PROMPT.md` is loaded once at
import in `agent_loop.py` and passed as `system` on conversational calls. `skill_learning.py`
passes no system prompt — its calls must return strict JSON, and persona/tone instructions
would corrupt structured output. Edits to the file take effect on the next agent restart.

**The sidecar owns the mic and the speaker.** `core/voice_session.py` captures audio and
`tts.speak()` plays replies through sounddevice; no audio crosses the WebSocket (the old
base64 `speech` message is gone). This is load-bearing, not incidental: the session is
half-duplex — captured frames are discarded while `speak()` is playing plus a 300 ms tail —
and only the process driving the speaker knows exactly when that is. Route any new playback
through `speak()` or the mic will hear it and act on it. Voice commands run on the most
recently connected UI (`server._primary()`); every spoken input, push-to-talk or session,
goes through `server.handle_transcript()`. The session's frame source is injectable, which
is how `scripts/voice_sim.py` tests it without a mic — keep it that way.

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
- Renderer is plain ES modules — no framework, no build step. `preload.js` is the only bridge;
  the page has no Node access.
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
- Local state stays out of git: `preferences.json`, `trusted_commands.json`, `voice_settings.json`, `logs/`,
  `skills/generated/`, `assets/voices/`, `.env`, `plans/`.
