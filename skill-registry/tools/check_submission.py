"""Structural check of a skill submission (step 1 of the PR workflow).

    check_submission.py --base <dir> --head <dir> --leutheria <dir>
                        [--catalog <path>] --author <login> --out <result.json>

--base is the registry at the PR's base commit, --head the PR's tree (read
as data only, never executed), --leutheria a checkout of Leutheria at the
pinned ref (LEUTHERIA_REF) whose validate.py and tool_catalog.json are used;
--catalog overrides the catalog file (tests).

A valid submission adds exactly one file, skills/<name>/<content_hash>.json,
changes and deletes nothing, contains no symlinks, is at most 64 KiB of
strict JSON, and passes validate.validate_shared against the catalog. A new
version of an existing name must come from that name's author.

Writes {"ok", "errors": [...], "path", "name", "content_hash",
"catalog_version"} and exits 0 when ok, 1 when rejected, 2 on a usage error.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import common


def _tree(root: Path) -> dict:
    """relative posix path -> bytes, or None for a symlink / non-file."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts[:1] == (".git",):
            continue
        dirnames[:] = [d for d in dirnames if not (rel_dir == Path(".") and d == ".git")]
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                out[(rel_dir / d).as_posix()] = None
                dirnames.remove(d)
        for f in filenames:
            if rel_dir == Path(".") and f == ".git":
                continue  # a worktree's .git file
            p = Path(dirpath) / f
            rel = (rel_dir / f).as_posix()
            out[rel] = None if p.is_symlink() or not p.is_file() else p.read_bytes()
    return out


def _authors(base: Path, name: str) -> set:
    folder = base / "reviews" / name
    authors = set()
    if folder.is_dir():
        for record in folder.glob("*.json"):
            try:
                authors.add(json.loads(record.read_text(encoding="utf-8"))["author"])
            except (OSError, ValueError, KeyError, TypeError):
                authors.add(None)  # unreadable record: nobody may claim the name automatically
    return authors


def check(base: Path, head: Path, leutheria: Path, catalog_path: Path, author: str) -> dict:
    result = {"ok": False, "errors": [], "path": None, "name": None, "content_hash": None, "catalog_version": None}
    errors = result["errors"]

    validate = common.load_validate(leutheria)
    catalog = common.load_catalog(catalog_path)
    try:
        kwargs = validate.catalog_validation_kwargs(catalog)
    except validate.SkillValidationError as e:
        errors.append(f"tool catalog unusable: {e}")
        return result
    result["catalog_version"] = catalog["catalog_version"]

    before, after = _tree(base), _tree(head)
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p] or after[p] is None)
    for p in deleted:
        errors.append(f"deletes {p}: published files are never removed (ask for a yank)")
    for p in modified:
        errors.append(f"changes {p}: published files are never edited (submit a new version)")
    for p in added:
        if after[p] is None:
            errors.append(f"adds {p} as a symlink or special file")
        elif not p.startswith("skills/"):
            errors.append(f"adds {p} outside skills/")
    if len(added) != 1:
        errors.append(f"a submission adds exactly one file; this adds {len(added)}")
    if errors:
        return result

    rel = added[0]
    result["path"] = rel
    try:
        definition = common.check_skill_file(validate, kwargs, rel, after[rel])
    except common.RegistryError as e:
        errors.append(str(e))
        return result
    result["name"], result["content_hash"] = definition["name"], definition["content_hash"]

    owners = _authors(base, definition["name"])
    if owners and owners != {author}:
        errors.append(f"{definition['name']!r} is already published by someone else; choose another name")
    result["ok"] = not errors
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Structural check of a registry submission")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--leutheria", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--author", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for d in (args.base, args.head, args.leutheria):
        if not d.is_dir():
            print(f"not a directory: {d}", file=sys.stderr)
            return 2
    result = check(args.base, args.head, args.leutheria,
                   args.catalog or common.default_catalog_path(args.leutheria), args.author)
    common.write_json(args.out, result)
    print(("OK  " if result["ok"] else "REJECT  ") + (result["path"] or "") +
          "".join(f"\n  - {e}" for e in result["errors"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
