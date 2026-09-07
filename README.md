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
renderer/
  - chat UI, mic button (records + sends audio), confirm buttons, plays TTS audio

        |
        |  ws://127.0.0.1:8765 -- JSON messages, both directions
        v

Python Agent (agent/agent/)
---------------------------
server.py
  - routes every message by type
  - logs everything to logs/events.jsonl

  "audio" message
    -> audio_input.py -> stt.py (Whisper) -> transcript text
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
    checks SKILLS first, then TOOLS
    |                          |
    v                          v
  skills/registry.py      tools/registry.py
    setup_project            open_app
    (calls dispatch()        create_folder
     again for its own       list_files
     sub-steps)              run_command  (destructive)
                                  |
                                  v
                         destructive? -> send "confirmation_required" AND
                         speak the prompt, pause until a "confirm" message
                         answers it -- which the next transcribed voice
                         reply can supply directly (voice_confirm.py)

  once agent_loop gets a final text reply (no more tool calls):
    |
    v
  tts.py (Piper) -> "speech" message -> Electron plays it

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
  main.js             Spawns the Python agent, connects to it over WebSocket, hosts the dev window
  preload.js           Bridges main-process logs/chat/confirmations/audio into the renderer safely
  renderer/            Chat UI: message bubbles, confirm Approve/Decline buttons, mic button,
                        collapsible raw log

agent/                Python sidecar
  SYSTEM_PROMPT.md     Persona/tone/behavior -- loaded once, passed as `system` on every
                       conversational LLM call. Hand-editable, no restart-the-world needed
                       beyond restarting the agent process.
  trusted_commands.json  Permanently-trusted (tool, args) pairs (gitignored -- local
                       state, not source). See trust.py below.
  .venv/              Virtualenv (gitignored)
  agent/
    core/
      server.py        WebSocket server, message routing (max_size=20MB -- a long spoken
                       reply's audio can exceed the 1MB library default, see BUGS.md #7)
      dispatcher.py     Routes {tool, args} payloads to tool functions; destructive tools
                        pause for a live confirmation round-trip before running, unless
                        trust.py says this exact (tool, args) pair is already trusted --
                        the confirmation prompt is also spoken via tts.py's speak()
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
      tts.py            Local Piper ("en_US-amy-medium" voice) speech synthesis; speak()
                        is shared by server.py (final replies) and dispatcher.py
                        (spoken confirmation prompts)
      skill_learning.py Checks logs/events.jsonl for a matching past run, then asks
                        Claude to generalize into a skill definition -- confidently
                        from two examples if repeated, or from one example (flagging
                        uncertain params as clarifying questions) if this is the first
      logging_util.py   Appends every message/decision/tool call to logs/events.jsonl
    tools/
      registry.py       TOOLS dict (fn, safety, description, input_schema) + anthropic_tool_schemas()
      open_app.py        create_folder.py    list_files.py       run_command.py
      get_clipboard.py   set_clipboard.py    open_url.py         show_notification.py
      take_screenshot.py read_file.py        append_text.py      trash_file.py
      move_file.py       copy_file.py        list_running_apps.py
      get_battery_status.py  get_disk_space.py  get_current_datetime.py
                        One flat .py file per tool -- no per-tool folders/__init__.py;
                        that packaging turned out to be more annoying to navigate than
                        it was worth for single-function tools, so it was reverted.
    skills/
      registry.py       SKILLS dict + anthropic_skill_schemas(); loads generated/*.json at
                        startup, plus register_skill() to add a new one at runtime
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

This spawns the Python agent automatically, connects to it, and opens the dev log
window — you'll see the full round-trip logged there (and in the terminal).

> **Known quirk in some sandboxed/CI shells:** if `ELECTRON_RUN_AS_NODE=1` is set in
> your environment, Electron silently runs as plain Node and the app APIs
> (`app`, `BrowserWindow`, `nativeImage`, etc.) will be `undefined`. If you hit that,
> run instead:
> ```bash
> env -u ELECTRON_RUN_AS_NODE npm start
> ```
> A normal terminal usually won't have this set at all.

`main.js` sends one hardcoded test command on connect (`create_folder` targeting
`~/Desktop/leutheria-test`) — check the dev window/terminal log and confirm the folder
actually appears on your Desktop. That proves the raw tool-dispatch path.

The dev window also has a **text input at the bottom** — type a plain-English command
and press Enter to exercise the real LLM agentic loop (item #4): the text goes to Claude
along with the tool schemas, and Claude can call a tool, see the result, and call
another tool before giving a final answer — it's a loop, not a single shot. Each tool
call in the chain shows as its own `🔧` line in the chat, followed by the final reply.
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

Click the 🎤 button next to the input to start recording, click it again (now showing ⏹,
pulsing red) to stop. macOS will prompt for microphone access the first time — grant it to
Electron. On stop, the clip is sent to the agent, transcribed locally with Whisper, and the
transcript appears as your own chat bubble (prefixed `🎤`) before the normal agentic loop
runs on it exactly as if you'd typed it. There's no wake word or true "hold to talk" yet —
it's click-to-start/click-to-stop for now, which is simpler to get right than global hotkey
hold-detection and matches the roadmap's "push-to-talk is fine for now" scope.

### Voice output (TTS)

Every final text reply is also spoken out loud automatically — no toggle needed. The
agent synthesizes it locally with Piper right after sending the text response, and the
audio plays as soon as it arrives in the renderer. This applies to typed, spoken, and
tool-triggered replies alike, since it's wired in at the point where the final response
is sent, not per input method. If synthesis fails for any reason, it's logged
(`tts_error` in `agent/logs/events.jsonl`) but never blocks or delays the text reply
itself — TTS is additive, not a dependency of the core loop.

Note: `piper-tts` is GPL-3.0-or-later licensed. Fine for local development; worth
revisiting the licensing implications before any public release/distribution.

## Running the Python agent standalone (no Electron)

Useful for testing tools directly without going through Electron.

```bash
cd agent
./.venv/bin/python -m agent
```

This starts the WebSocket server at `ws://127.0.0.1:8765` and prints `AGENT_READY`
once it's up. Leave it running in one terminal, then in another terminal use a small
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

1. Any request that finishes with 2+ tool calls and isn't already covered by an existing
   skill is a candidate. `agent/core/skill_learning.py` checks `agent/logs/events.jsonl`
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
this whole exchange is automatic: the confirmation shows up as a chat bubble with four
buttons — Approve once / Approve (session) / Approve (always) / Decline
(`app/renderer/`) — and clicking one sends the `confirm` message for you.

**Trust (`agent/core/trust.py`)**: `remember: "session"` or `"always"` tells
`dispatcher.dispatch()` to skip the confirmation prompt entirely next time — but only for
the *exact same* `(tool, args)` pair, checked via `trust.is_trusted()` before a
confirmation is even requested. `"session"` is an in-memory set that resets when the
agent restarts; `"always"` additionally persists to `agent/trusted_commands.json`
(gitignored — local state, not source) and is checked on every future startup. This is
deliberately narrow: trusting one specific `git init` in one specific directory can never
blanket-trust a *different* `rm` command elsewhere just because both happen to be
`run_command` calls — only a genuinely identical repeated invocation skips the prompt.

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
# -> {"type": "transcript", "text": "open the calculator app"}   (sent first)
# -> {"type": "response", "text": "Calculator is now open...", "trace": [...]}  (final)
```

`format` can be any container ffmpeg can decode (the Electron app sends `webm`, since
that's what the browser's `MediaRecorder` produces by default). No API key needed for
this part — Whisper runs fully locally.

### Voice output protocol

After any `{"type": "response", ...}` with non-empty text, a `speech` message follows
automatically with the synthesized audio as base64 WAV:

```python
{"type": "response", "text": "12 × 7 = **84**", "trace": []}
{"type": "speech", "data": "<base64-encoded WAV audio>"}
```

There's no request for this — it's always sent after a text reply, for every input
method (typed, voice, or a tool result reached via either). No API key needed; Piper
runs fully locally.

### Skill proposal protocol

May follow a `response` (after any `speech` message) whenever a multi-step (2+ tool call)
success isn't already covered by an existing skill -- on the first occurrence as well as
repeats, not just repeats. Fully independent of the request/response cycle -- nothing is
waiting on this, so it can arrive or not without affecting anything else:

```python
{"type": "response", "text": "...", "trace": [...]}          # the actual answer, unaffected
{"type": "speech", "data": "..."}                              # if any
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

# only on approval:
{"type": "skill_saved", "name": "..."}
```

A decline is just logged (`skill_declined` in `agent/logs/events.jsonl`) -- no reply is
sent back for it, since there's nothing further to report. `uncertain_params` is always
present (possibly empty) so the client doesn't need to special-case a missing key.

## Logs

Every message received, every LLM decision, and every tool call (regardless of whether
it came from the LLM loop or a direct `{"tool": ...}` message) is appended as one JSON
line to `agent/logs/events.jsonl` (gitignored). Event types: `message_in`, `message_out`,
`llm_turn` (what Claude decided that turn), `tool_call` (which tool ran, with what args
and result), `stt_transcript` (what Whisper heard), `tts_error` (a synthesis failure --
non-fatal, just skipped), `confirmation_requested`/`tool_declined` (the destructive-tool
confirmation flow). Tail it live while testing:

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
