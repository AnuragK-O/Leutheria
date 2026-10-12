from agent.skills import dev_common as dc

GITIGNORE = """\
__pycache__/
*.py[cod]
.venv/
venv/
.env
.pytest_cache/
.mypy_cache/
build/
dist/
*.egg-info/
.DS_Store
"""


def files_for(name: str) -> dict:
    # The name has already passed dev_common's folder-name check, so it holds
    # nothing that needs escaping inside a Python string or a Markdown line.
    return {
        "main.py": (
            f'"""{name}: entry point."""\n'
            "\n\n"
            "def main() -> None:\n"
            f'    print("Hello from {name}!")\n'
            "\n\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        ),
        "README.md": (
            f"# {name}\n\n"
            "## Run\n\n"
            "```\npython3 main.py\n```\n\n"
            "## Set up a virtual environment\n\n"
            "```\npython3 -m venv .venv\nsource .venv/bin/activate\n"
            "pip install -r requirements.txt\n```\n"
        ),
        "requirements.txt": "# One package per line, for example: requests>=2.31\n",
        ".gitignore": GITIGNORE,
    }


async def run(args: dict, websocket, pending: dict) -> dict:
    """Scaffold a runnable Python project in a new folder.

    create_folder and write_new_file are safe and never replace anything, so
    the scaffold itself runs without a prompt. The two opt-in extras (git
    init, a virtual environment) are shell commands, so each goes through
    run_command and is asked about separately.
    """
    steps = dc.Steps(websocket, pending)
    root, name, error = dc.project_root(args)
    if error:
        return steps.fail(error)

    failure = await dc.scaffold(steps, root, files_for(name))
    if failure:
        return failure

    extras = []
    if dc.flag(args, "git_init"):
        failure = await dc.optional_command(steps, "git init", f"git -C {dc.q(root)} init", root)
        if failure:
            return failure
        extras.append("git initialized")
    if dc.flag(args, "create_venv"):
        failure = await dc.optional_command(
            steps, "creating the virtual environment", f"python3 -m venv {dc.q(root / '.venv')}", root
        )
        if failure:
            return failure
        extras.append("virtual environment created")
    app = dc.text_arg(args, "open_in")
    if app:
        failure = await dc.open_in_app(steps, root, app, root)
        if failure:
            return failure
        extras.append(f"opened in {app}")

    message = f"Python project {name} created at {root} with main.py, a README, requirements.txt and a gitignore"
    message += (", " + ", ".join(extras) if extras else "") + "."
    return steps.done(message, path=str(root))
