"""Interactive CLI test client for Leutheria.

Allows testing the agent, its autonomy modes (Guide, Copilot, Autopilot),
and human corrections directly in the terminal via WebSocket.
"""

import asyncio
import json
import sys
import websockets

WS_URL = "ws://127.0.0.1:8765"


async def main():
    print("\n" + "=" * 62)
    print("  LEUTHERIA CLI TEST RUNNER")
    print("  Self-Improving Desktop Agent Flywheel")
    print("=" * 62)
    print("  Connecting to ws://127.0.0.1:8765...")

    try:
        ws = await websockets.connect(WS_URL)
    except Exception as err:
        print(f"\n❌ Could not connect to {WS_URL} ({err})")
        print("Tip: Run `python -m agent` in another terminal or `npm start` / `npm run web` in app/.\n")
        return

    print("✅ Connected to Leutheria agent backend!\n")
    print("Commands:")
    print("  /mode <guide|copilot|autopilot>  - Switch autonomy mode")
    print("  /skills                          - List learned and system skills")
    print("  /metrics                         - Show agent telemetry & flywheel stats")
    print("  /exit                            - Quit CLI\n")

    current_mode = "copilot"
    request_counter = 1

    async def sender():
        nonlocal current_mode, request_counter
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, f"Leutheria [{current_mode.upper()}] > ")
            except (EOFError, KeyboardInterrupt):
                break

            text = line.strip()
            if not text:
                continue

            if text in ("/exit", "/quit", "exit", "quit"):
                break

            if text.startswith("/mode"):
                parts = text.split()
                if len(parts) > 1 and parts[1].lower() in ("guide", "copilot", "autopilot"):
                    current_mode = parts[1].lower()
                    print(f"🔄 Switched mode to: {current_mode.upper()}\n")
                else:
                    print("Usage: /mode <guide|copilot|autopilot>\n")
                continue

            if text == "/skills":
                req_id = str(request_counter)
                request_counter += 1
                await ws.send(json.dumps({"type": "inventory", "request_id": req_id}))
                continue

            if text == "/metrics":
                req_id = str(request_counter)
                request_counter += 1
                await ws.send(json.dumps({"type": "get_metrics", "request_id": req_id}))
                continue

            # Regular command prompt to agent
            await ws.send(json.dumps({"type": "command", "text": text, "mode": current_mode}))

    async def receiver():
        loop = asyncio.get_running_loop()
        try:
            async for msg_raw in ws:
                data = json.loads(msg_raw)
                msg_type = data.get("type")

                if msg_type == "timeline_event":
                    event = data.get("event")
                    desc = data.get("description", "")
                    print(f"  [timeline] {event}: {desc}")

                elif msg_type == "confirmation_required":
                    conf_id = data.get("id")
                    tool = data.get("tool")
                    args = data.get("args")
                    risk = data.get("risk", "medium")
                    prompt = data.get("prompt", "")

                    print(f"\n⚠️  [CONFIRMATION REQUIRED - Risk: {risk.upper()}]")
                    print(f"    Action: {tool}")
                    print(f"    Args:   {json.dumps(args)}")
                    if prompt:
                        print(f"    Reason: {prompt}")

                    choice = await loop.run_in_executor(
                        None, input, "    [A]pprove  [R]eject  [C]orrect  -> "
                    )
                    choice = choice.strip().lower()

                    if choice.startswith("c"):
                        correction = await loop.run_in_executor(
                            None, input, "    Enter correction instruction: "
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "confirm",
                                    "id": conf_id,
                                    "approved": False,
                                    "remember": True,
                                    "correction": correction.strip(),
                                }
                            )
                        )
                        print("    ✏️ Correction submitted!\n")
                    elif choice.startswith("a"):
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "confirm",
                                    "id": conf_id,
                                    "approved": True,
                                    "remember": False,
                                }
                            )
                        )
                        print("    ✅ Action approved.\n")
                    else:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "confirm",
                                    "id": conf_id,
                                    "approved": False,
                                    "remember": False,
                                }
                            )
                        )
                        print("    ❌ Action rejected.\n")

                elif msg_type == "skill_proposed":
                    prop_id = data.get("id")
                    name = data.get("name")
                    desc = data.get("description")
                    steps = data.get("steps", [])
                    print(f"\n💡 [SKILL PROPOSED] '{name}'")
                    print(f"    Description: {desc}")
                    print(f"    Steps ({len(steps)}): {[s.get('tool') for s in steps]}")
                    save_choice = await loop.run_in_executor(
                        None, input, "    Save this skill? [Y/n]: "
                    )
                    approved = not save_choice.strip().lower().startswith("n")
                    await ws.send(
                        json.dumps(
                            {
                                "type": "skill_response",
                                "id": prop_id,
                                "approved": approved,
                                "resolutions": {},
                            }
                        )
                    )

                elif msg_type == "skill_saved":
                    print(f"\n🎉 [SKILL SAVED] '{data.get('name')}' is now registered in the SQLite skill store!\n")

                elif msg_type == "control_result":
                    if "skills" in data:
                        skills = data.get("skills", [])
                        tools = data.get("tools", [])
                        print(f"\n📦 INVENTORY: {len(skills)} Skills, {len(tools)} Tools")
                        for s in skills:
                            print(f"   • [{s.get('source', 'learned')}] {s.get('name')}: {s.get('description')}")
                        print()
                    elif "metrics" in data:
                        print(f"\n📊 METRICS:\n{json.dumps(data.get('metrics', {}), indent=2)}\n")

                elif msg_type == "response":
                    text = data.get("text", "")
                    trace = data.get("trace", [])
                    skill_used = data.get("skill_used")
                    print(f"\n🤖 Leutheria: {text}")
                    if skill_used:
                        print(f"   [Reused Skill: {skill_used}]")
                    if trace:
                        print(f"   [Trace: {len(trace)} steps recorded in SQLite]")
                    print()

                elif msg_type == "error":
                    print(f"\n❌ Error: {data.get('text')}\n")

        except asyncio.CancelledError:
            pass

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())

    done, pending_tasks = await asyncio.wait(
        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending_tasks:
        t.cancel()

    await ws.close()
    print("\nGoodbye!\n")


if __name__ == "__main__":
    asyncio.run(main())
