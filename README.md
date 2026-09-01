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
                         destructive? -> send "confirmation_required",
                         pause until a "confirm" message answers it

  once agent_loop gets a final text reply (no more tool calls):
    |
    v
  tts.py (Piper) -> "speech" message -> Electron plays it
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
  .venv/              Virtualenv (gitignored)
  agent/
    core/
      server.py        WebSocket server, message routing
      dispatcher.py     Routes {tool, args} payloads to tool functions; destructive tools
                        pause for a live confirmation round-trip before running
      llm.py            LLMBackend interface (LLMTurn) -- swappable backend seam
      llm_anthropic.py  AnthropicBackend: calls claude-opus-5 with the tool schemas
      agent_loop.py     The Claude <-> tool-dispatch loop: keeps calling tools and feeding
                        results back until Claude gives a final answer (capped at 6 turns)
      audio_input.py    Decodes a base64 audio payload to a temp file, hands off to stt.py
      stt.py            Local Whisper ("base" model) transcription
      tts.py            Local Piper ("en_US-amy-medium" voice) speech synthesis
      logging_util.py   Appends every message/decision/tool call to logs/events.jsonl
    tools/
      basic.py          open_app, create_folder, list_files, run_command
      registry.py       TOOLS dict (fn, safety, description, input_schema) + anthropic_tool_schemas()
    skills/
      setup_project.py   First hand-written skill: create_folder + git init + open in VS Code
                        as one multi-step procedure, exposed to the LLM as a single tool
      registry.py       SKILLS dict + anthropic_skill_schemas() -- same shape as tools/registry.py
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

Example calls for each (swap the `tool`/`args` fields in the snippet above):

```python
{"type": "command", "tool": "open_app", "args": {"name": "Calculator"}}
{"type": "command", "tool": "create_folder", "args": {"path": "~/Desktop/leutheria-test"}}
{"type": "command", "tool": "list_files", "args": {"path": "~/Desktop"}}
{"type": "command", "tool": "run_command", "args": {"cmd": "echo hi"}}   # pauses for confirmation
```

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
`agent/skills/setup_project.py`) demonstrates this: it runs `create_folder` (safe, no
prompt) then two `run_command` calls (`git init`, then opening the folder in VS Code),
each requiring its own approval.

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
{"type": "confirm", "id": "<uuid>", "approved": true}   # or false

# 4. only now does the agent send the real result:
{"type": "tool_result", "tool": "run_command", "result": {"ok": true, "stdout": "hi", "stderr": ""}}
```

This works identically whether the destructive tool was called directly or picked mid-loop
by Claude — in the LLM path, a decline gets fed back to Claude as a failed tool result, so
it can explain what happened instead of the connection just hanging. In the Electron app,
this whole exchange is automatic: the confirmation shows up as a chat bubble with
Approve/Decline buttons (`app/renderer/`), and clicking one sends the `confirm` message
for you.

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
