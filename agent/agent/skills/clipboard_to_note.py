from agent.skills import dev_common as dc

DEFAULT_PATH = "~/Notes/clipboard.txt"
MAX_CHARS = 50000  # a clipboard can hold a whole document; a note shouldn't


async def run(args: dict, websocket, pending: dict) -> dict:
    """Append what's on the clipboard, timestamped, to a running text file.
    get_clipboard reads and append_text can only add, so nothing asks."""
    steps = dc.Steps(websocket, pending)
    notes_path = dc.text_arg(args, "notes_path", DEFAULT_PATH)
    label = " ".join(dc.text_arg(args, "label").split())[:120]

    clip = await steps.run("get_clipboard", {})
    if not clip.get("ok"):
        return steps.step_failed("reading the clipboard", clip)
    text = (clip.get("text") or "").strip()
    if not text:
        return steps.fail("The clipboard is empty, or holds something that is not text, so nothing was saved.")
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]

    header = f"[{dc.stamp()}]" + (f" {label}" if label else "")
    # One line stays on the header's line; anything longer goes underneath,
    # with a blank line after so entries stay apart in the file.
    entry = f"{header} {text}" if "\n" not in text else f"{header}\n{text}\n"
    result = await steps.run("append_text", {"path": notes_path, "text": entry})
    if not result.get("ok"):
        return steps.step_failed("saving the note", result)

    size = f"{len(text)} characters"
    message = f"Saved the clipboard, {size}, to {notes_path}."
    if truncated:
        message += f" It was longer than {MAX_CHARS} characters, so the rest was left out."
    return steps.done(message, path=notes_path, characters=len(text), truncated=truncated)
