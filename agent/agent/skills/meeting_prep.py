from agent.skills.system_steps import Steps, StepFailed, battery_phrase, invalid, number

# For a call: below this the volume is treated as "you won't hear them".
QUIET_LEVEL = 20
AUDIBLE_LEVEL = 40


async def run(args: dict, websocket, pending: dict) -> dict:
    """Get the Mac ready for a meeting. What that means depends on the kind:

      call       -- you need to hear and be connected: check the network and
                    battery, and make sure sound is on (unmute; raise the
                    volume to 40 only if it is below 20).
      in_person  -- the Mac should be quiet: mute it, check the battery.

    Then, if asked, open the meeting app and set a timer for when it should
    end. Steps vary with the arguments; every one goes through dispatch().
    """
    kind = args.get("kind") or "call"
    if kind not in ("call", "in_person"):
        return invalid("kind must be call or in_person")
    app = args.get("app") or ""
    if not isinstance(app, str):
        return invalid("app must be text")
    try:
        minutes = number(args, "minutes", None, 0.001, 480)
    except ValueError as e:
        return invalid(str(e))

    steps = Steps(websocket, pending)
    said = []
    try:
        if kind == "call":
            network = await steps.run("get_network_status")
            if not network.get("connected"):
                said.append("Heads up: there is no network connection.")
            elif not network.get("internet"):
                said.append("Heads up: the network is connected but the internet is not reachable.")
            else:
                said.append(f"You're online over {network.get('kind')}.")

        volume = await steps.run("get_volume")
        if kind == "in_person":
            if not volume.get("muted"):
                await steps.run("set_volume", {"muted": True})
            said.append("Sound is muted.")
        elif volume.get("muted") or volume.get("level", 0) < QUIET_LEVEL:
            change = {"muted": False}
            if volume.get("level", 0) < QUIET_LEVEL:
                change["level"] = AUDIBLE_LEVEL
            after = await steps.run("set_volume", change)
            said.append(f"Sound is on at {after.get('level')} percent.")
        else:
            said.append(f"Sound is on at {volume.get('level')} percent.")

        battery = await steps.run("get_battery_status")
        said.append(battery_phrase(battery, low=30)[0])

        if app.strip():
            await steps.run("open_app", {"name": app.strip()})
            said.append(f"{app.strip()} is opening.")
        if minutes is not None:
            timer = await steps.run(
                "set_timer",
                {"minutes": minutes, "label": "meeting", "message": "Your meeting time is up."},
            )
            said.append(f"I'll tell you at {timer['ends_at']} when the time is up.")
    except StepFailed as failure:
        return steps.failed(failure)
    return steps.done(" ".join(said), kind=kind)
