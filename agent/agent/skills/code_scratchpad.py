import re

from agent.skills import dev_common as dc

DEFAULT_FOLDER = "~/Scratch"

# language -> (file extension, starter content). Each starter is a complete,
# runnable (or valid) file in that language, as small as that can be.
# No shell: open_with refuses .sh files (macOS can run them), so a shell
# scratch file could be created but never opened.
LANGUAGES = {
    "python": ("py", 'print("scratch")\n'),
    "javascript": ("js", 'console.log("scratch");\n'),
    "typescript": ("ts", 'console.log("scratch");\n'),
    "html": ("html", '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <title>Scratch</title>\n</head>\n<body>\n</body>\n</html>\n'),
    "css": ("css", "body {\n}\n"),
    "json": ("json", "{}\n"),
    "sql": ("sql", "SELECT 1;\n"),
    "markdown": ("md", "# Scratch\n"),
    "text": ("txt", "\n"),
    "swift": ("swift", 'print("scratch")\n'),
    "go": ("go", 'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("scratch")\n}\n'),
    "rust": ("rs", 'fn main() {\n    println!("scratch");\n}\n'),
    "c": ("c", '#include <stdio.h>\n\nint main(void) {\n    puts("scratch");\n    return 0;\n}\n'),
    "cpp": ("cpp", '#include <iostream>\n\nint main() {\n    std::cout << "scratch" << std::endl;\n    return 0;\n}\n'),
    "ruby": ("rb", 'puts "scratch"\n'),
}
ALIASES = {
    "py": "python", "js": "javascript", "node": "javascript", "ts": "typescript",
    "md": "markdown", "txt": "text",
    "plain": "text", "c++": "cpp", "golang": "go", "rb": "ruby", "rs": "rust",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


async def run(args: dict, websocket, pending: dict) -> dict:
    """Make a new throwaway code file and open it in an editor. The file is
    new (timestamped unless named, and write_new_file never replaces one),
    so nothing asks. It is created and opened, never run."""
    steps = dc.Steps(websocket, pending)
    language = dc.text_arg(args, "language", "python").lower()
    language = ALIASES.get(language, language)
    if language not in LANGUAGES:
        return steps.fail(f"I can start a scratch file in: {', '.join(LANGUAGES)}.")
    extension, starter = LANGUAGES[language]

    name = dc.text_arg(args, "name")
    if name:
        if not _NAME_RE.match(name):
            return steps.fail("The file name must be letters, digits, dots, dashes or underscores.")
        # The extension always comes from the language, never from the name:
        # "notes.command" must not become a file macOS would offer to run.
        suffix = f".{extension}"
        stem = name[: -len(suffix)] if name.lower().endswith(suffix) else name
        if "." in stem:
            return steps.fail(f"Give the name without an extension; a {language} file gets {suffix} added.")
        filename = stem + suffix
    else:
        filename = f"scratch-{dc.stamp('%Y%m%d-%H%M%S')}.{extension}"
    folder = dc.expand(dc.text_arg(args, "folder", DEFAULT_FOLDER))
    target = folder / filename
    if target.exists():
        return steps.fail(f"{target} already exists. I never overwrite a file, so give a different name.")

    content = starter
    if dc.flag(args, "from_clipboard"):
        clip = await steps.run("get_clipboard", {})
        if not clip.get("ok"):
            return steps.step_failed("reading the clipboard", clip)
        text = clip.get("text") or ""
        if not text.strip():
            return steps.fail("The clipboard is empty, so there is nothing to put in the file.")
        content = text if text.endswith("\n") else text + "\n"

    result = await steps.run("create_folder", {"path": str(folder)})
    if not result.get("ok"):
        return steps.step_failed("creating the folder", result)
    result = await steps.run("write_new_file", {"path": str(target), "content": content})
    if not result.get("ok"):
        return steps.step_failed("writing the file", result)

    app = dc.text_arg(args, "open_in", dc.DEFAULT_EDITOR)
    failure = await dc.open_in_app(steps, target, app, target)
    if failure:
        return failure
    filled = " with the clipboard's contents" if dc.flag(args, "from_clipboard") else ""
    return steps.done(f"Created {target}{filled} and opened it in {app}.", path=str(target), language=language)
