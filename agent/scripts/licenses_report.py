"""Regenerate the generated tables in THIRD_PARTY_LICENSES.md.

    cd agent
    ./.venv/bin/python scripts/licenses_report.py           # print the tables
    ./.venv/bin/python scripts/licenses_report.py --write   # replace them in the file

Python: every package named in requirements.txt plus everything those pull
in (the installed dependency closure, extras ignored), read from installed
metadata -- so run it with the venv the app ships. requirements-piper.txt is
reported in its own table because it is NOT shipped.

npm: app/package.json's dependencies plus their dependency closure, read
from app/node_modules/*/package.json (run `npm install` in app/ first).

Only the text between the BEGIN/END GENERATED markers is replaced; the
hand-written sections (models, fonts, Qt, decisions) are left alone. A
licence the metadata doesn't state is printed as "UNKNOWN -- verify".
Reads files only; no network.
"""

import json
import re
import sys
from importlib import metadata
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
REPO = AGENT_DIR.parent
TARGET = REPO / "THIRD_PARTY_LICENSES.md"
BEGIN, END = "<!-- BEGIN GENERATED: licenses_report.py -->", "<!-- END GENERATED -->"

# Classifier -> short name, for packages that only declare licences that way.
_CLASSIFIER = re.compile(r"^License :: (?:OSI Approved :: )?(.+)$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> list:
    names = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(re.split(r"[<>=!~;\[ ]", line, 1)[0])
    return names


def _deps(dist) -> list:
    """Requirements that apply on this machine: extras and markers for other
    platforms (CUDA wheels on Linux, Windows-only colorama) are skipped."""
    from packaging.requirements import Requirement

    out = []
    for raw in dist.requires or []:
        req = Requirement(raw)
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        out.append(req.name)
    return out


def _license(dist) -> str:
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    raw = (meta.get("License") or "").strip()
    classifiers = [m.group(1) for c in meta.get_all("Classifier") or [] if (m := _CLASSIFIER.match(c))]
    # A License field holding the whole licence text is useless in a table.
    if raw and len(raw) <= 60 and "\n" not in raw and raw.upper() != "UNKNOWN":
        return raw
    # Some put the licence's full text there; its first line names it.
    first = raw.splitlines()[0].strip() if raw else ""
    if first and len(first) <= 40 and "license" in first.lower():
        return first
    if classifiers:
        return " / ".join(classifiers)
    return "UNKNOWN -- verify"


def _url(dist) -> str:
    meta = dist.metadata
    urls = [u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []]
    preferred = [u for u in urls if any(k in u for k in ("github.com", "gitlab", "source", "repo"))]
    return (preferred or urls or [meta.get("Home-page") or ""])[0] or f"https://pypi.org/project/{meta['Name']}/"


def python_closure(requirements: Path, exclude=()) -> list:
    seen, rows = set(_norm(n) for n in exclude), []
    queue = _requirement_names(requirements)
    while queue:
        name = queue.pop(0)
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            rows.append((name, "not installed", "UNKNOWN -- verify", f"https://pypi.org/project/{name}/"))
            continue
        rows.append((dist.metadata["Name"], dist.version, _license(dist), _url(dist)))
        queue.extend(_deps(dist))
    return sorted(rows, key=lambda r: r[0].lower())


def npm_closure(app_dir: Path) -> list:
    modules = app_dir / "node_modules"
    package = json.loads((app_dir / "package.json").read_text())
    queue, seen, rows = list(package.get("dependencies", {})), set(), []
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        manifest = modules / name / "package.json"
        if not manifest.exists():
            rows.append((name, "not installed", "UNKNOWN -- verify", f"https://www.npmjs.com/package/{name}"))
            continue
        data = json.loads(manifest.read_text())
        licence = data.get("license")
        if isinstance(licence, dict):
            licence = licence.get("type")
        repo = data.get("repository")
        if isinstance(repo, dict):
            repo = repo.get("url")
        url = (repo or data.get("homepage") or f"https://www.npmjs.com/package/{name}")
        url = re.sub(r"^git\+|\.git$", "", url).replace("git://", "https://")
        if url.startswith("github:") or re.match(r"^[\w-]+/[\w.-]+$", url):
            url = "https://github.com/" + url.split(":", 1)[-1]
        rows.append((name, data.get("version", "?"), licence or "UNKNOWN -- verify", url))
        queue.extend(data.get("dependencies", {}))
    return sorted(rows, key=lambda r: r[0].lower())


def _table(rows) -> str:
    lines = ["| Package | Version | Licence | Source |", "|---|---|---|---|"]
    lines += [f"| {n} | {v} | {lic.replace('|', '/')} | {u} |" for n, v, lic, u in rows]
    return "\n".join(lines)


def report() -> str:
    shipped = python_closure(AGENT_DIR / "requirements.txt")
    shipped_names = [r[0] for r in shipped]
    piper = python_closure(AGENT_DIR / "requirements-piper.txt", exclude=shipped_names)
    npm = npm_closure(REPO / "app")
    return "\n\n".join([
        BEGIN,
        f"### Python packages shipped (agent/requirements.txt and dependencies: {len(shipped)})",
        _table(shipped),
        "### Python packages NOT shipped (agent/requirements-piper.txt, optional, user-installed)",
        _table(piper),
        f"### npm packages shipped (app/package.json dependencies and their dependencies: {len(npm)})",
        _table(npm),
        END,
    ])


def main() -> int:
    text = report()
    if "--write" not in sys.argv:
        print(text)
        return 0
    current = TARGET.read_text()
    if BEGIN not in current or END not in current:
        print(f"{TARGET} has no generated markers; add {BEGIN} ... {END} first", file=sys.stderr)
        return 1
    head, rest = current.split(BEGIN, 1)
    tail = rest.split(END, 1)[1]
    TARGET.write_text(head + text + tail)
    print(f"updated {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
