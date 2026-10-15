"""Build index.json from skills/, reviews/ and yanked.json.

    build_index.py --repo <dir> --leutheria <dir> [--catalog <path>]
                   [--out <repo>/index.json] [--now <iso>]

Every skill file with a review record is re-checked exactly as a submission
is (strict JSON, path/name/hash, validate_shared against the catalog) and
listed with its recorded version, author, published_at and review, its byte
sha256 and size, a plain summary, requires and has_run_command, and whether
it is yanked. A file without a record is pending and left out. Any other
inconsistency -- a record without its file, a mismatched record, a
non-yanked file that no longer validates (say a tool it uses was removed:
yank it), one name with records by two authors -- fails the build rather
than silently dropping a skill or letting one author's version update
another's.

The index carries generated_at and a signed expires_at (generated_at +
common.INDEX_VALIDITY_DAYS) so a client can tell a frozen copy from a live
one: a mirror or attacker replaying an old signed index can hide a yank only
until it expires. If the result equals the existing index apart from those
two timestamps, the file is left untouched (so its signature stays valid and
nothing is committed) -- unless --refresh, which the daily scheduled publish
passes so the published index never gets near its expiry.

    build_index.py ... [--refresh]
Exit 0 on success, 1 on any inconsistency.
"""

import argparse
import datetime
import sys
from pathlib import Path

import common
from record_review import now_iso


def _yanked(repo: Path) -> set:
    doc = common.read_json(repo / "yanked.json", default={"yanked": []})
    return {(y["name"], y["content_hash"]) for y in doc.get("yanked", [])}


def build(repo: Path, leutheria: Path, catalog_path: Path, now: str):
    validate = common.load_validate(leutheria)
    catalog = common.load_catalog(catalog_path)
    kwargs = validate.catalog_validation_kwargs(catalog)
    yanked = _yanked(repo)
    errors, pending, entries = [], [], []

    files = sorted(p.relative_to(repo).as_posix() for p in (repo / "skills").glob("*/*") if p.name != ".gitkeep")
    records = sorted(p.relative_to(repo).as_posix() for p in (repo / "reviews").glob("*/*.json"))
    record_keys = {tuple(r.split("/")[1:3]) for r in records}

    for rel in records:
        name, file_name = rel.split("/")[1:3]
        if not (repo / "skills" / name / file_name).is_file():
            errors.append(f"{rel} has no skill file")
    for rel in files:
        name, file_name = rel.split("/")[1:3]
        if (name, file_name) not in record_keys:
            pending.append(rel)
            continue
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            errors.append(f"{rel} is not a regular file")
            continue
        data = path.read_bytes()
        digest = file_name[: -len(".json")]
        is_yanked = (name, digest) in yanked
        try:
            definition = common.check_skill_file(validate, kwargs, rel, data)
        except common.RegistryError as e:
            if not is_yanked:
                errors.append(str(e))
                continue
            definition = None
        record = common.read_json(repo / "reviews" / name / file_name)
        if record.get("name") != name or record.get("content_hash") != digest:
            errors.append(f"reviews/{name}/{file_name} does not describe {rel}")
            continue
        requires = definition["requires"] if definition else []
        entries.append({
            "name": name,
            "content_hash": digest,
            "version": record["version"],
            "path": rel,
            "sha256": common.sha256_bytes(data),
            "size": len(data),
            "summary": common.summary_of(definition["description"]) if definition else "",
            "requires": requires,
            "has_run_command": "run_command" in requires,
            "author": record["author"],
            "published_at": record["published_at"],
            "review": record["review"],
            "yanked": is_yanked,
        })
    for name, digest in sorted(yanked - {(e["name"], e["content_hash"]) for e in entries}):
        errors.append(f"yanked.json names {name}/{digest[:12]}, which is not published")
    authors = {}
    for e in entries:
        authors.setdefault(e["name"], set()).add(str(e["author"]).lower())
    for name, who in sorted(authors.items()):
        if len(who) > 1:
            errors.append(f"{name} has records by more than one author ({len(who)}); a name has one owner")
    seen = {}
    for e in entries:
        key = (e["name"], e["version"])
        if key in seen:
            errors.append(f"{e['name']} has two files recorded as version {e['version']}")
        seen[key] = e
    entries.sort(key=lambda e: (e["name"], e["version"]))
    expires = common.parse_iso_z(now) + datetime.timedelta(days=common.INDEX_VALIDITY_DAYS)
    index = {
        "format": common.INDEX_FORMAT,
        "generated_at": now,
        "expires_at": common.format_iso_z(expires),
        "catalog_version": catalog["catalog_version"],
        "skills": entries,
    }
    return index, errors, pending


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the registry index")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--leutheria", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--now", default=None)
    parser.add_argument("--refresh", action="store_true",
                        help="rewrite even if only the timestamps would change (the daily re-sign)")
    args = parser.parse_args()
    out = args.out or args.repo / "index.json"
    try:
        index, errors, pending = build(args.repo, args.leutheria,
                                       args.catalog or common.default_catalog_path(args.leutheria),
                                       args.now or now_iso())
    except (common.RegistryError, OSError, ValueError, KeyError) as e:
        print(f"FAIL  {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for p in pending:
        print(f"pending (no review record): {p}")
    if errors:
        for e in errors:
            print(f"FAIL  {e}", file=sys.stderr)
        return 1
    if out.exists() and not args.refresh:
        try:
            old = common.read_json(out)
            stamps = {"generated_at": None, "expires_at": None}
            if "expires_at" in old and {**old, **stamps} == {**index, **stamps}:
                print(f"unchanged  {out} ({len(index['skills'])} entries)")
                return 0
        except ValueError:
            pass
    out.write_bytes(common.render_index(index))
    print(f"wrote {out} ({len(index['skills'])} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
