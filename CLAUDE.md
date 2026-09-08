# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Leutheria: a macOS voice-controlled assistant. An **Electron shell** (`app/`) spawns a
**Python sidecar** (`agent/`) and talks to it over `ws://127.0.0.1:8765` with JSON messages.
All agent work — STT (Whisper), TTS (Piper), LLM tool-calling (Anthropic `claude-opus-5`),
tool/skill execution — happens Python-side; Electron is UI + process supervision only.

`README.md` is the detailed reference (full wire protocol per message type, tool table,
troubleshooting). Read the relevant section there before changing protocol or agent internals.
`plans/` (gitignored) holds ROADMAP.md, NOTES.md (open questions), and BUGS.md (numbered
bugs referenced from code comments and the README).

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

**Safety tiers are structural, not advisory.** `run_command` is the only `"destructive"` tool
because it's the only unbounded one; everything else is `"safe"` *by construction* —
`trash_file` uses the recoverable Trash, `move_file`/`copy_file` refuse to overwrite,
`append_text` can't truncate. A new tool that can destroy data must either be redesigned to
be structurally safe or tagged `"destructive"`.

**Preferences gate twice.** `core/preferences.py` overrides (`enabled`, `requires_confirmation`)
are applied both when building the schemas sent to Claude *and* independently in
`dispatch()` — so a disabled capability is neither describable nor runnable, even if baked
into a stale plan or a learned skill's step.

**`agent/logs/events.jsonl` is the single source of truth** for the Library's usage counts,
the Activity history, and skill-learning's repeat detection. Anything that should show up in
the UI must be logged via `core/logging_util.py`. Skill learning checks the log for a past
matching run *before* the current request logs its own entry (checking after self-matches —
BUGS.md #6).

**Management messages never touch the LLM.** `inventory` / `set_preference` / `delete_skill` /
`revoke_trust` are answered inline in the read loop by `core/control.py`, matched by
`request_id`, so UI reads can't block or be blocked by a running command.

**The system prompt applies to conversation only.** `agent/SYSTEM_PROMPT.md` is loaded once at
import in `agent_loop.py` and passed as `system` on conversational calls. `skill_learning.py`
passes no system prompt — its calls must return strict JSON, and persona/tone instructions
would corrupt structured output. Edits to the file take effect on the next agent restart.

TTS and skill learning are additive: a failure in either is logged and skipped, never blocking
or delaying the text reply.

## Conventions

- One flat `.py` per tool in `agent/agent/tools/` (no per-tool packages — that was tried and
  reverted), plus an entry in `tools/registry.py` with `fn`, `safety`, `description`,
  `input_schema`. Descriptions are prompt surface: write them for the model.
- Hand-written skills get their own module with `run(args, websocket, pending)` and a
  `SKILLS` entry; they are code, so the UI can disable but not delete them.
- Trust is keyed on the exact `(tool, args)` pair, never the tool name alone (`core/trust.py`).
- Renderer is plain ES modules — no framework, no build step. `preload.js` is the only bridge;
  the page has no Node access.
- Expand `~` with `Path(...).expanduser()` before building any shell string — `/bin/sh`'s `cd`
  won't expand it inside quotes (this caused a real silent `git init` failure).
- Replies are spoken aloud, so agent-facing prose (SYSTEM_PROMPT.md, tool descriptions the
  model echoes) should avoid markdown — TTS reads the punctuation literally.
- Local state stays out of git: `preferences.json`, `trusted_commands.json`, `logs/`,
  `skills/generated/`, `assets/voices/`, `.env`, `plans/`.
