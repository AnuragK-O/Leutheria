"""Export the tool catalog the public skill registry validates against.

    cd agent
    ./.venv/bin/python scripts/export_tool_catalog.py           # rewrite agent/tool_catalog.json
    ./.venv/bin/python scripts/export_tool_catalog.py --check   # exit 1 if the committed file is stale
    ./.venv/bin/python scripts/export_tool_catalog.py --out <path>

The catalog is the live tool registry reduced to what a shared skill can be
judged by (tools only -- generated skills may call tools only): per tool its
name, safety, input_schema, paths, outbound and untrusted_output, plus the
built-in skill names (a shared skill may not take one), catalog_version
(skills.validate.catalog_digest) and the Leutheria commit it was exported
on. --check compares everything but that commit, so it fails exactly when a
tool changed and nobody re-exported. Run it after changing any tool.

Importing the registries reads this checkout's skills/generated/ (as the app
does at startup); the event log is pointed at a temp dir first so nothing is
logged.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))
CATALOG_PATH = AGENT_DIR / "tool_catalog.json"


def build_catalog(ref: str) -> dict:
    from agent.core import logging_util

    tmp = Path(tempfile.mkdtemp(prefix="leutheria_catalog_"))
    logging_util.LOG_DIR = tmp / "logs"
    logging_util.LOG_FILE = logging_util.LOG_DIR / "events.jsonl"

    from agent.skills import registry as skills_registry
    from agent.skills import validate
    from agent.tools.registry import TOOLS

    tools = []
    for name in sorted(TOOLS):
        entry = TOOLS[name]
        tools.append({
            "name": name,
            "safety": entry["safety"],
            "input_schema": entry.get("input_schema", {"type": "object", "properties": {}}),
            "paths": dict(sorted((entry.get("paths") or {}).items())),
            "outbound": entry.get("outbound"),
            "untrusted_output": bool(entry.get("untrusted_output", False)),
        })
    catalog = {
        "format": validate.CATALOG_FORMAT,
        "tools": tools,
        "builtin_skills": sorted(skills_registry.BUILTIN_SKILLS),
    }
    # Round-trip so the digest is over exactly what gets written.
    catalog = json.loads(json.dumps(catalog, allow_nan=False))
    catalog["catalog_version"] = validate.catalog_digest(catalog)
    catalog["leutheria_ref"] = ref
    return catalog


def git_ref() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=AGENT_DIR, capture_output=True, text=True, timeout=10, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def render(catalog: dict) -> str:
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed catalog is stale")
    parser.add_argument("--out", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()

    fresh = build_catalog(git_ref())
    if args.check:
        try:
            committed = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"STALE  {args.out}: unreadable ({e})")
            return 1
        strip = lambda c: {k: v for k, v in c.items() if k != "leutheria_ref"}  # noqa: E731
        if strip(committed) != strip(fresh):
            print(f"STALE  {args.out} (committed {committed.get('catalog_version', '?')[:12]}, "
                  f"live {fresh['catalog_version'][:12]}): run scripts/export_tool_catalog.py")
            return 1
        print(f"OK  {args.out} is current ({fresh['catalog_version'][:12]}, {len(fresh['tools'])} tools)")
        return 0
    args.out.write_text(render(fresh), encoding="utf-8")
    print(f"wrote {args.out} ({fresh['catalog_version'][:12]}, {len(fresh['tools'])} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
