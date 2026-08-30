# Leutheria

A voice-controlled AI assistant that controls your computer — opening apps, pulling live
data, and executing tasks via natural speech.

Architecture: a **Node/Electron shell** (`/app`) drives a **Python sidecar process**
(`/agent`) that does the actual agent work (tool execution, and eventually STT/TTS and
LLM tool-calling). The two talk over a local WebSocket.

See `plans/ROADMAP.md` (gitignored, local-only) for the current build plan.

## Project structure

```
app/                  Electron shell
  main.js             Spawns the Python agent, connects to it over WebSocket, hosts the dev window
  preload.js           Bridges main-process logs into the renderer safely
  renderer/            Dev log window (plain HTML/JS)

agent/                Python sidecar
  .venv/              Virtualenv (gitignored)
  agent/
    core/
      server.py        WebSocket server, message routing
      dispatcher.py     Routes {tool, args} payloads to tool functions, enforces safety tiers
      llm.py            LLMBackend interface (ToolCall / TextResponse) -- swappable backend seam
      llm_anthropic.py  AnthropicBackend: calls claude-opus-5 with the tool schemas
    tools/
      basic.py          open_app, create_folder, list_files, run_command
      registry.py       TOOLS dict (fn, safety, description, input_schema) + anthropic_tool_schemas()
    skills/             (empty for now — future saved skills live here)
```

## Setup

Requires Homebrew Python 3.12 (the system/Xcode Python 3.9 is too old) and Node.

```bash
# Python side
cd agent
/opt/homebrew/bin/python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt   # or: pip install websockets anthropic python-dotenv

# Node side
cd ../app
npm install
```

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
and press Enter to exercise the real LLM tool-call path (item #4): the text goes to
Claude along with the tool schemas, and whatever it decides (a tool call, or just a
reply) gets dispatched/logged the same way. Try things like:
- `open the Calculator app` → should actually open Calculator
- `what is 12 times 7?` → should just get a text reply, no tool call
- `delete everything on my desktop` → should get **blocked**, since that maps to the
  destructive `run_command` tool, which the dispatcher refuses to run

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
| `run_command` | `{"cmd": "echo hi"}` | **destructive** | currently **blocked** — dispatcher refuses to run any destructive tool until step 5 (confirmation UX) is built |

Example calls for each (swap the `tool`/`args` fields in the snippet above):

```python
{"type": "command", "tool": "open_app", "args": {"name": "Calculator"}}
{"type": "command", "tool": "create_folder", "args": {"path": "~/Desktop/leutheria-test"}}
{"type": "command", "tool": "list_files", "args": {"path": "~/Desktop"}}
{"type": "command", "tool": "run_command", "args": {"cmd": "echo hi"}}   # expected: blocked
```

Sending a `text` field (no `tool`) goes through the LLM tool-call path instead — Claude
sees the tool schemas and either calls one or just replies:

```python
{"type": "command", "text": "open the Calculator app"}
# -> {"type": "tool_result", "tool": "open_app", "result": {"ok": true, "message": "opened Calculator"}}

{"type": "command", "text": "what is 12 times 7?"}
# -> {"type": "response", "text": "12 × 7 = **84**"}
```

This requires `ANTHROPIC_API_KEY` to be resolvable (see API key section above) — without
it, the agent will error when a text command comes in (the `{"tool": ...}` path above
doesn't need a key at all, since it skips the LLM entirely).

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
