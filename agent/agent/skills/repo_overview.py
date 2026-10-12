from agent.skills import dev_common as dc

README_NAMES = ("README.md", "README", "README.txt", "README.rst", "readme.md")
README_CHARS = 1500
MAX_LISTED = 40

# Files whose presence says what kind of project this is.
MARKERS = {
    "package.json": "Node",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "setup.py": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "CMakeLists.txt": "C or C++ with CMake",
    "Package.swift": "Swift",
    "pom.xml": "Java with Maven",
    "build.gradle": "Java or Kotlin with Gradle",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "index.html": "a static web page",
}


async def run(args: dict, websocket, pending: dict) -> dict:
    """Describe a repository: git state, recent commits, what's at the top
    level, and the start of its README. Three read-only tools, no prompt."""
    steps = dc.Steps(websocket, pending)
    path = dc.text_arg(args, "path")
    if not path:
        return steps.fail("I need the folder of the repository.")

    status = await steps.run("git_status", {"path": path, "log_count": 5})
    if not status.get("ok"):
        return steps.step_failed("reading the git status", status)
    root = status.get("root") or str(dc.expand(path))

    listing = await steps.run("list_files", {"path": root})
    if not listing.get("ok"):
        return steps.step_failed("listing the folder", listing)
    names = [n for n in listing.get("files", []) if n != ".git"]

    kinds = []
    for marker, kind in MARKERS.items():
        if marker in names and kind not in kinds:
            kinds.append(kind)

    excerpt = None
    readme = next((n for n in README_NAMES if n in names), None)
    if readme:
        content = await steps.run("read_file", {"path": f"{root}/{readme}"})
        if not content.get("ok"):
            return steps.step_failed(f"reading {readme}", content)
        excerpt = content.get("content", "")[:README_CHARS]

    parts = [status.get("message", "").rstrip(".")]
    parts.append(f"{len(names)} items at the top level")
    if kinds:
        parts.append("looks like " + " and ".join(kinds[:3]))
    if not readme:
        parts.append("no README")
    return steps.done(
        ". ".join(p for p in parts if p) + ".",
        root=root,
        branch=status.get("branch"),
        clean=status.get("clean"),
        changed_files=status.get("changed_files"),
        commits=status.get("commits"),
        remote_url=status.get("remote_url"),
        project_kinds=kinds,
        top_level=names[:MAX_LISTED],
        top_level_truncated=len(names) > MAX_LISTED,
        readme_excerpt=excerpt,
    )
