import json
import re

from agent.skills import dev_common as dc

GITIGNORE = """\
node_modules/
npm-debug.log*
.env
dist/
coverage/
.DS_Store
"""


def package_name(name: str) -> str:
    """npm package names are lower-case and URL-safe; the folder name needn't be."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return cleaned[:200] or "app"


def files_for(name: str) -> dict:
    package = {
        "name": package_name(name),
        "version": "0.1.0",
        "description": "",
        "private": True,
        "main": "index.js",
        "scripts": {"start": "node index.js"},
    }
    return {
        "package.json": json.dumps(package, indent=2) + "\n",
        "index.js": f"console.log({json.dumps(f'Hello from {name}!')});\n",
        "README.md": f"# {name}\n\n## Run\n\n```\nnpm start\n```\n",
        ".gitignore": GITIGNORE,
    }


async def run(args: dict, websocket, pending: dict) -> dict:
    """Scaffold a runnable Node.js project in a new folder. Same shape as
    new_python_project: the scaffold is safe and unprompted, the opt-in
    extras (git init, npm install) each go through run_command and ask."""
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
    if dc.flag(args, "install"):
        failure = await dc.optional_command(
            steps, "npm install", f"npm install --prefix {dc.q(root)}", root
        )
        if failure:
            return failure
        extras.append("npm install run")
    app = dc.text_arg(args, "open_in")
    if app:
        failure = await dc.open_in_app(steps, root, app, root)
        if failure:
            return failure
        extras.append(f"opened in {app}")

    message = f"Node project {name} created at {root} with package.json, index.js, a README and a gitignore"
    message += (", " + ", ".join(extras) if extras else "") + "."
    return steps.done(message, path=str(root))
