"""Verify a registry the way the client will: signature, then every file.

    verify_index.py --repo <dir> --keys <keys.json> [--leutheria <dir> [--catalog <path>]] [--now <iso>]

1. index.json.sig verifies over the exact bytes of index.json with a key in
   --keys ({"keys": [{key_id, public_key}]}), selected by key_id;
2. the index is format 1, its expires_at is well-formed, after generated_at,
   at most common.INDEX_MAX_VALIDITY_DAYS after it, and not yet past (as of
   --now, default the clock); every entry's path is skills/<name>/<hash>.json
   matching its name and content_hash;
3. each file's size and sha256 match the entry, it is strict JSON, and its
   content_hash recomputes (needs --leutheria for validate.py);
4. with --leutheria, non-yanked files pass validate_shared against the
   catalog, and the index's catalog_version is that catalog's.
Any failure exits 1 -- never a partial pass.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import common


def verify(repo: Path, keys: list, leutheria: Path = None, catalog_path: Path = None, now=None) -> list:
    errors = []
    index_bytes = (repo / "index.json").read_bytes()
    try:
        sig_doc = json.loads((repo / "index.json.sig").read_text(encoding="utf-8"))
        common.verify_signature(index_bytes, sig_doc, keys)
    except (common.RegistryError, OSError, ValueError, KeyError, TypeError) as e:
        return [f"signature: {e}"]
    index = json.loads(index_bytes)
    if index.get("format") != common.INDEX_FORMAT or not isinstance(index.get("skills"), list):
        return ["index is not format 1"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stale = common.expiry_problem(index, now)
    if stale:
        errors.append(stale)

    validate = kwargs = None
    if leutheria:
        validate = common.load_validate(leutheria)
        catalog = common.load_catalog(catalog_path or common.default_catalog_path(leutheria))
        kwargs = validate.catalog_validation_kwargs(catalog)
        if index.get("catalog_version") != catalog["catalog_version"]:
            errors.append("index catalog_version is not the pinned catalog's")

    for entry in index["skills"]:
        rel = entry.get("path", "")
        try:
            name, digest = common.split_skill_path(rel)
            if (name, digest) != (entry.get("name"), entry.get("content_hash")):
                raise common.RegistryError(f"{rel} does not match its entry's name/content_hash")
            path = repo / rel
            if path.is_symlink() or not path.is_file():
                raise common.RegistryError(f"{rel} missing")
            data = path.read_bytes()
            if len(data) != entry.get("size") or common.sha256_bytes(data) != entry.get("sha256"):
                raise common.RegistryError(f"{rel}: bytes do not match the index (size/sha256)")
            if validate is not None:
                if entry.get("yanked"):
                    definition = common.parse_skill_bytes(data)
                    if validate.content_hash(definition) != digest:
                        raise common.RegistryError(f"{rel}: content_hash does not recompute")
                else:
                    common.check_skill_file(validate, kwargs, rel, data)
        except (common.RegistryError, OSError, KeyError, TypeError) as e:
            errors.append(str(e))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the registry index and files")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument("--leutheria", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--now", help="check expiry as of this YYYY-MM-DDTHH:MM:SSZ (tests); default the clock")
    args = parser.parse_args()
    try:
        keys = common.read_json(args.keys)["keys"]
        now = common.parse_iso_z(args.now) if args.now else None
        errors = verify(args.repo, keys, args.leutheria, args.catalog, now)
    except (common.RegistryError, OSError, ValueError, KeyError) as e:
        errors = [f"{type(e).__name__}: {e}"]
    for e in errors:
        print(f"FAIL  {e}", file=sys.stderr)
    if not errors:
        print("OK  index signature and every file verify")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
