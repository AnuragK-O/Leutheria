"""Install a shared skill from the public registry by voice or chat.

Fetches and verifies the newest non-withdrawn version (core/registry_client
.py), then asks through the dispatcher's own confirmation card, whose args
list every step with its arguments, flag run_command steps and show the
review verdict and author. Nothing is written unless that card is approved.
The approval is never remembered: a card for installing is not something to
trust "always". Updating is the same: a new version is a new approval.
"""

import asyncio
import re

from agent.core.logging_util import log_event


async def run(args: dict, websocket, pending: dict) -> dict:
    from agent.core import registry_client
    from agent.core.dispatcher import _confirm

    raw = str(args.get("name") or "").strip().lower()
    name = re.sub(r"[\s-]+", "_", raw)
    if not name:
        return {"ok": False, "error": "say which shared skill to install"}

    loop = asyncio.get_running_loop()
    try:
        entry, definition = await loop.run_in_executor(None, registry_client.resolve, name, None)
    except registry_client.RegistryError as e:
        return {"ok": False, "error": str(e)}
    finally:
        registry_client.apply_withdrawals(registry_client._current_index())
    conflict = registry_client.author_conflict(entry)
    if conflict:
        return conflict

    card =registry_client.approval_card(entry, definition)
    steps = len(card["steps"])
    intro = (
        f"Install the shared skill {card['name']}, version {card['version']}, by {card['author']}? "
        f"It has {steps} step{'s' if steps != 1 else ''}"
        + (", including shell commands that will ask every time they run" if card["has_run_command"] else "")
        + ". Every step is on the card. Should I install it?"
    )
    approved, _ = await _confirm("install_shared_skill", card, websocket, pending, intro)
    if not approved:
        log_event("shared_skill_install_declined", name=card["name"], content_hash=card["content_hash"])
        return {"ok": False, "error": "the install was declined (or the card timed out)"}
    result = registry_client.commit_install(entry, definition)
    if result.get("ok"):
        result["message"] = (
            f"Installed {card['name']}. The first time it runs, each of its steps asks first."
        )
    return result
