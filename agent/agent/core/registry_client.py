"""Client for the public shared-skill registry: fetch and verify the signed
index, preview / install / update shared skills, and build a share
submission for one of the user's own learned skills.

Trust chain (any failure refuses the whole operation, never a partial one):

  1. index.json and index.json.sig are fetched from the ONE registry host
     (REGISTRY_URL; this is not fetch_url and takes no URL from anyone);
  2. the signature is Ed25519 over the exact index bytes, by a key listed
     in core/registry_keys.py (matched by key_id). No key configured = no
     registry: nothing is listed, previewed or installed;
  3. the index is structurally checked, and refused if it is older than
     one already verified here (a replayed old index would un-yank);
  4. a skill file's bytes must match the index's size and sha256, it must
     carry no `source`, its content_hash must recompute and equal the
     index's, and skills/validate.py must pass with this machine's tools.

Then installing: only through an approval that showed every step (the UI's
card, which sends registry_install afterwards, or install_shared_skill's
_confirm card). The skill is written with source "imported:<hash>" and a
registry record beside it (skills/registry.py); the model sees a description
built here, never the author's. Updates are new approvals; a yanked version
stays installed and is shown as withdrawn. At most MAX_IMPORTED installed.

Test-only: LEUTHERIA_REGISTRY_URL replaces the host (http only to loopback)
and, only together with it, LEUTHERIA_REGISTRY_TEST_KEY ("<key_id>:<base64
raw public key>") adds one accepted key. Both exist for
scripts/registry_client_check.py and a local registry; neither is for use
by a real install.

Threads: fetching and verifying (get_index, fetch_verified_skill) run on a
worker thread (control messages via run_in_executor, search_shared_skills as
a worker-thread tool) and touch only this module's cache, under _lock.
Everything that reads or changes SKILLS (install, list, share) runs on the
loop thread.
"""

import asyncio
import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.core import path_policy, registry_keys, registry_text
from agent.core.logging_util import log_event
from agent.skills import registry, validate
from agent.skills.validate import HASH_RE, NAME_RE, SkillValidationError

REGISTRY_URL = "https://raw.githubusercontent.com/AnuragK-O/leutheria-skills/main/"
SHARE_REPO = "https://github.com/AnuragK-O/leutheria-skills"
SHARE_BRANCH = "main"
TEST_URL_ENV = "LEUTHERIA_REGISTRY_URL"
TEST_KEY_ENV = "LEUTHERIA_REGISTRY_TEST_KEY"
MAX_IMPORTED_ENV = "LEUTHERIA_MAX_IMPORTED_SKILLS"

TIMEOUT = 6.0  # seconds, per socket operation
DEADLINE = 15.0  # seconds, whole fetch
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_SIG_BYTES = 4096
MAX_SKILL_BYTES = registry.MAX_FILE_BYTES
INDEX_FRESH_SECONDS = 60
MAX_SHARE_URL = 8000
DEFAULT_MAX_IMPORTED = 20

CONTROL_TYPES = {"registry_list", "registry_preview", "registry_install", "registry_share_preview", "registry_share"}
# Not for an overlay connection: it never shows the install card or the
# share preview these come after.
UI_ONLY_TYPES = {"registry_install", "registry_share"}

_PATH_RE = re.compile(r"index\.json|index\.json\.sig|skills/[a-z][a-z0-9_]{0,63}/[0-9a-f]{64}\.json")
_ISO_RE = re.compile(r"\d{4}-\d\d-\d\dT[0-9:.]+(Z|[+-]\d\d:\d\d)?")
_tls = ssl.create_default_context()
_lock = threading.Lock()
_memory = {"index": None, "verified_at": 0.0}


class RegistryError(Exception):
    pass


# --- configuration ------------------------------------------------------------


def registry_base() -> str:
    override = os.environ.get(TEST_URL_ENV)
    if not override:
        return REGISTRY_URL
    parts = urllib.parse.urlsplit(override)
    if parts.scheme == "https" and parts.hostname:
        pass
    elif parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        pass
    else:
        raise RegistryError(f"{TEST_URL_ENV} must be https, or http to this machine")
    return override if override.endswith("/") else override + "/"


def accepted_keys() -> dict:
    """key_id -> Ed25519PublicKey. Production keys always; the env test key
    only while the test URL override is set."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    entries = list(registry_keys.PRODUCTION_KEYS)
    test = os.environ.get(TEST_KEY_ENV)
    if test and os.environ.get(TEST_URL_ENV):
        key_id, _, b64 = test.partition(":")
        entries.append({"key_id": key_id, "public_key": b64})
    keys = {}
    for entry in entries:
        try:
            raw = base64.b64decode(entry["public_key"], validate=True)
            # key_id is derived from the key (the registry's signer writes
            # sha256(raw public key)[:16]); a configured id that disagrees
            # is a mistake in the key list, so the key is not used.
            key_id = hashlib.sha256(raw).hexdigest()[:16]
            if str(entry["key_id"]) != key_id:
                raise ValueError("key_id does not match the key")
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw)
        except (KeyError, ValueError, TypeError, binascii.Error):
            log_event("registry_key_invalid", key_id=str(entry.get("key_id")))
    return keys


def max_imported() -> int:
    try:
        return max(0, min(50, int(os.environ.get(MAX_IMPORTED_ENV, DEFAULT_MAX_IMPORTED))))
    except ValueError:
        return DEFAULT_MAX_IMPORTED


# --- fetching -----------------------------------------------------------------


def _fetch(path: str, cap: int) -> bytes:
    """GET one fixed path under the registry base. No redirects (a 3xx is a
    failure), at most `cap` bytes, and DEADLINE covers the whole call: a
    watchdog shuts the socket when it passes, so a server dripping bytes
    can't hold the call open (the fetch_url pattern, review M2)."""
    if not _PATH_RE.fullmatch(path):
        raise RegistryError(f"refusing to fetch {path!r}")
    base = urllib.parse.urlsplit(registry_base())
    https = base.scheme == "https"
    port = base.port or (443 if https else 80)
    request_path = base.path.rstrip("/") + "/" + path
    if https:
        conn = http.client.HTTPSConnection(base.hostname, port, timeout=TIMEOUT, context=_tls)
    else:
        conn = http.client.HTTPConnection(base.hostname, port, timeout=TIMEOUT)
    expired = threading.Event()
    opened = []

    def _kill():
        # The socket captured at connect: http.client drops conn.sock once a
        # response says the connection will close, while the response
        # still reads from it (found by the slow-server check).
        expired.set()
        for sock in opened:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(DEADLINE, _kill)
    watchdog.daemon = True
    watchdog.start()
    started = time.monotonic()
    try:
        conn.connect()
        opened.append(conn.sock)
        if expired.is_set():
            raise RegistryError(f"{path}: the registry took longer than {DEADLINE:g} s")
        conn.request("GET", request_path, headers={"User-Agent": "Leutheria/1.0 (skill registry)", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if response.status != 200:
            raise RegistryError(f"{path}: HTTP {response.status}")
        length = response.getheader("Content-Length")
        if length and length.isdigit() and int(length) > cap:
            raise RegistryError(f"{path} is larger than {cap} bytes")
        data = bytearray()
        while True:
            chunk = response.read(min(65536, cap + 1 - len(data)))
            if expired.is_set() or time.monotonic() - started > DEADLINE:
                raise RegistryError(f"{path}: the registry took longer than {DEADLINE:g} s")
            if not chunk:
                break
            data += chunk
            if len(data) > cap:
                raise RegistryError(f"{path} is larger than {cap} bytes")
        return bytes(data)
    except RegistryError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as e:
        if expired.is_set():
            raise RegistryError(f"{path}: the registry took longer than {DEADLINE:g} s") from None
        raise RegistryError(f"{path}: {type(e).__name__}: {e}") from None
    finally:
        watchdog.cancel()
        conn.close()


# --- the index ----------------------------------------------------------------


def _reject_constant(constant):
    raise ValueError(f"{constant} is not plain JSON")


def verify_index(index_bytes: bytes, sig_bytes: bytes) -> dict:
    """Signature first, then structure. Returns the parsed index."""
    from cryptography.exceptions import InvalidSignature

    keys = accepted_keys()
    if not keys:
        raise RegistryError("no registry signing key is configured, so shared skills are unavailable")
    try:
        sig = json.loads(sig_bytes.decode("utf-8"))
        key_id, signature = sig["key_id"], base64.b64decode(sig["sig"], validate=True)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        raise RegistryError("index signature file is malformed") from None
    key = keys.get(str(key_id))
    if key is None:
        raise RegistryError(f"index is signed with an unknown key ({str(key_id)[:40]})")
    try:
        key.verify(signature, index_bytes)
    except InvalidSignature:
        raise RegistryError("index signature does not verify") from None
    try:
        index = json.loads(index_bytes.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise RegistryError("index is not valid JSON") from None
    _check_index(index)
    return index


def _check_index(index) -> None:
    def bad(why):
        raise RegistryError(f"index is malformed: {why}")

    if not isinstance(index, dict) or index.get("format") != 1 or not isinstance(index.get("skills"), list):
        bad("not a format 1 index")
    if not isinstance(index.get("generated_at"), str) or not _ISO_RE.fullmatch(index["generated_at"]):
        bad("generated_at")
    seen = set()
    for i, e in enumerate(index["skills"]):
        if not isinstance(e, dict):
            bad(f"entry {i}")
        name, h = e.get("name"), e.get("content_hash")
        if not (isinstance(name, str) and NAME_RE.fullmatch(name) and isinstance(h, str) and HASH_RE.fullmatch(h)):
            bad(f"entry {i} name/content_hash")
        if (name, h) in seen:
            bad(f"duplicate {name} {h[:12]}")
        seen.add((name, h))
        if e.get("path") != f"skills/{name}/{h}.json":
            bad(f"{name} path")
        if not (isinstance(e.get("sha256"), str) and HASH_RE.fullmatch(e["sha256"])):
            bad(f"{name} sha256")
        for key in ("version", "size"):
            if not isinstance(e.get(key), int) or isinstance(e.get(key), bool) or e[key] < 1:
                bad(f"{name} {key}")
        if not isinstance(e.get("summary"), str) or len(e["summary"]) > 1000:
            bad(f"{name} summary")
        if not isinstance(e.get("requires"), list) or not all(isinstance(t, str) for t in e["requires"]):
            bad(f"{name} requires")
        for key in ("has_run_command", "yanked"):
            if not isinstance(e.get(key, False), bool):
                bad(f"{name} {key}")
        if not isinstance(e.get("author"), str) or not isinstance(e.get("review"), dict):
            bad(f"{name} author/review")


def _when(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cache_path() -> Path:
    return registry.imported_dir() / "_index_cache.json"


def _read_cache():
    """The cached index, re-verified (the cache file is a copy of what the
    registry served, not something trusted because it is on disk)."""
    path = _cache_path()
    try:
        if not path.exists() or path.is_symlink() or path.stat().st_size > 2 * MAX_INDEX_BYTES:
            return None
        cached = json.loads(path.read_text(encoding="utf-8"))
        return verify_index(base64.b64decode(cached["index"]), base64.b64decode(cached["sig"]))
    except (OSError, ValueError, KeyError, TypeError, binascii.Error, RegistryError):
        return None


def get_index(refresh: bool = False):
    """(index, stale_error). A fresh fetch unless one was verified in the
    last INDEX_FRESH_SECONDS; if fetching fails, the last verified index
    with the error, so the Library still shows what's installed.
    Raises RegistryError when there is nothing verified to show."""
    with _lock:
        if not refresh and _memory["index"] is not None and time.monotonic() - _memory["verified_at"] < INDEX_FRESH_SECONDS:
            return _memory["index"], None
        previous = _memory["index"] or _read_cache()
        try:
            index_bytes = _fetch("index.json", MAX_INDEX_BYTES)
            sig_bytes = _fetch("index.json.sig", MAX_SIG_BYTES)
            index = verify_index(index_bytes, sig_bytes)
            if previous is not None and _when(index["generated_at"]) < _when(previous["generated_at"]):
                raise RegistryError("the registry served an index older than one already seen here")
        except RegistryError as e:
            log_event("registry_index_failed", error=str(e))
            if previous is None or not accepted_keys():
                raise
            return previous, str(e)
        _memory.update(index=index, verified_at=time.monotonic())
        try:
            path = _cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"index": base64.b64encode(index_bytes).decode(), "sig": base64.b64encode(sig_bytes).decode()}))
            os.replace(tmp, path)
        except OSError as e:
            log_event("registry_cache_write_failed", error=str(e))
        log_event("registry_index_verified", skills=len(index["skills"]))
        return index, None


def reset_cache() -> None:
    """For checks: forget the in-memory index (the file cache stays)."""
    with _lock:
        _memory.update(index=None, verified_at=0.0)


def find_entry(index: dict, name: str, content_hash: str = None):
    """The entry for (name, content_hash), or, with no hash, the newest
    version of `name` that isn't yanked."""
    matches = [e for e in index["skills"] if e["name"] == name]
    if content_hash is not None:
        return next((e for e in matches if e["content_hash"] == content_hash), None)
    live = [e for e in matches if not e.get("yanked")]
    return max(live, key=lambda e: e["version"]) if live else None


def fetch_verified_skill(entry: dict) -> dict:
    """The skill file for an index entry, verified end to end, with its
    source set to imported:<hash>. Raises RegistryError."""
    data = _fetch(entry["path"], MAX_SKILL_BYTES)
    if len(data) != entry["size"]:
        raise RegistryError(f"{entry['name']}: file size does not match the index")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise RegistryError(f"{entry['name']}: file does not match the index's sha256")
    try:
        definition = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise RegistryError(f"{entry['name']}: file is not valid JSON") from None
    if not isinstance(definition, dict):
        raise RegistryError(f"{entry['name']}: file is not a skill")
    if "source" in definition or "source_signature" in definition:
        raise RegistryError(f"{entry['name']}: a registry file may not carry source fields")
    if definition.get("name") != entry["name"] or definition.get("content_hash") != entry["content_hash"]:
        raise RegistryError(f"{entry['name']}: file name or content_hash does not match the index")
    try:
        # The registry's own contract for a shared file (refuses source
        # fields, content_hash must recompute), from the registry engineer's
        # validate.py; then the local, full check with the source we stamp.
        shared_check = getattr(validate, "validate_shared", None)
        if shared_check is not None:
            shared_check(definition, **registry.validation_kwargs())
        definition["source"] = f"imported:{entry['content_hash']}"
        validate.validate(definition, **registry.validation_kwargs())
    except (SkillValidationError, KeyError, TypeError, AttributeError) as e:
        raise RegistryError(f"{entry['name']}: not a valid skill here: {e}") from None
    if sorted(set(entry["requires"])) != definition["requires"]:
        raise RegistryError(f"{entry['name']}: index requires does not match the steps")
    has_rc = any(s["tool"] == "run_command" for s in definition["steps"])
    if bool(entry.get("has_run_command")) != has_rc:
        raise RegistryError(f"{entry['name']}: index has_run_command does not match the steps")
    return definition


def resolve(name: str, content_hash: str = None):
    """(entry, definition) for an install or preview, both verified.
    Worker-thread safe."""
    index, _ = get_index(refresh=False)
    entry = find_entry(index, name, content_hash)
    if entry is None:
        raise RegistryError(f"no shared skill {name!r}" + (" with that content_hash" if content_hash else ""))
    if entry.get("yanked"):
        raise RegistryError(f"{name} version {entry['version']} was withdrawn from the registry")
    return entry, fetch_verified_skill(entry)


# --- install (loop thread) ------------------------------------------------------


def installed() -> dict:
    """name -> registry record, for every installed imported skill."""
    return {name: skill["registry"] for name, skill in registry.SKILLS.items() if skill.get("registry")}


def _review(entry: dict) -> dict:
    review = entry.get("review") or {}
    return {k: review.get(k) for k in ("verdict", "risk", "flags", "screened_by", "screen_version")}


def approval_card(entry: dict, definition: dict) -> dict:
    """What an install approval shows: every step and its args, run_command
    steps flagged, the review verdict, the author. Also what the voice
    card's args are."""
    from agent.tools.registry import TOOLS

    current = installed().get(entry["name"])
    return {
        "name": entry["name"],
        "version": entry["version"],
        "content_hash": entry["content_hash"],
        "author": registry_text.safe_author(entry.get("author")),
        "summary": registry_text.sanitize_prose(entry.get("summary")),
        "review": _review(entry),
        "has_run_command": any(s["tool"] == "run_command" for s in definition["steps"]),
        "steps": [
            {"tool": s["tool"], "args": s["args"], "safety": TOOLS[s["tool"]]["safety"], "run_command": s["tool"] == "run_command"}
            for s in definition["steps"]
        ],
        "input_schema": definition["input_schema"],
        "author_description": definition["description"],
        "model_description": registry_text.model_description(
            entry.get("author"), entry.get("summary"), definition["requires"],
            any(s["tool"] == "run_command" for s in definition["steps"]),
        ),
        "installed_version": current.get("version") if current else None,
        "replaces": current.get("content_hash") if current else None,
        "imported_count": len(installed()),
        "imported_max": max_imported(),
    }


def commit_install(entry: dict, definition: dict) -> dict:
    """Write and register an approved, verified skill. Loop thread only.
    An installed older version of the same name is replaced (an update the
    user just approved); anything else of that name is refused."""
    current = installed()
    if entry["name"] not in current and len(current) >= max_imported():
        return {"ok": False, "error": f"at most {max_imported()} shared skills can be installed; remove one first"}
    record = {
        "name": entry["name"],
        "content_hash": entry["content_hash"],
        "version": entry["version"],
        "author": registry_text.safe_author(entry.get("author")),
        "summary": entry.get("summary", "")[:1000],
        "review": _review(entry),
        "has_run_command": any(s["tool"] == "run_command" for s in definition["steps"]),
        "published_at": entry.get("published_at"),
        "installed_at": time.time(),
        "first_run_done": False,
    }
    result = registry.register_skill(definition, record=record, replace=entry["name"] in current)
    if result.get("ok") and not result.get("unchanged"):
        log_event("shared_skill_installed", name=entry["name"], content_hash=entry["content_hash"],
                  version=entry["version"], replaced=bool(result.get("replaced")))
    return result


# --- control messages -----------------------------------------------------------


def _list_result(index, stale_error) -> dict:
    mine = installed()
    newest = {}
    for e in index["skills"]:
        if not e.get("yanked") and (e["name"] not in newest or e["version"] > newest[e["name"]]["version"]):
            newest[e["name"]] = e
    by_key = {(e["name"], e["content_hash"]): e for e in index["skills"]}
    skills = []
    for name in sorted(newest):
        e = dict(newest[name])
        e["summary"] = registry_text.sanitize_prose(e.get("summary"))
        e["author"] = registry_text.safe_author(e.get("author"))
        rec = mine.get(name)
        e["installed_version"] = rec.get("version") if rec else None
        e["update_available"] = bool(rec) and rec.get("content_hash") != e["content_hash"] and e["version"] > (rec.get("version") or 0)
        skills.append(e)
    installed_list = []
    for name, rec in sorted(mine.items()):
        entry = by_key.get((name, rec.get("content_hash")))
        installed_list.append({
            **rec,
            "withdrawn": bool(entry and entry.get("yanked")),
            "in_index": entry is not None,
            "update_available": bool(newest.get(name)) and newest[name]["content_hash"] != rec.get("content_hash")
            and newest[name]["version"] > (rec.get("version") or 0),
        })
    result = {"ok": True, "index_ok": True, "skills": skills, "installed": installed_list,
              "imported_max": max_imported(), "generated_at": index.get("generated_at")}
    if stale_error:
        result.update(stale=True, error=stale_error)
    return result


async def handle_control(payload: dict) -> dict:
    """Answer one registry_* control message. Run as a background task by
    server.py (these can wait on the network); the network and verification
    part runs on a worker thread, anything touching SKILLS on the loop."""
    kind = payload.get("type")
    loop = asyncio.get_running_loop()
    try:
        if kind == "registry_list":
            try:
                index, stale = await loop.run_in_executor(None, get_index, bool(payload.get("refresh")))
            except RegistryError as e:
                return {"ok": False, "index_ok": False, "error": str(e), "skills": [],
                        "installed": [{**rec, "withdrawn": False, "in_index": False, "update_available": False}
                                      for _, rec in sorted(installed().items())],
                        "imported_max": max_imported()}
            return _list_result(index, stale)

        if kind in ("registry_preview", "registry_install"):
            name, content_hash = payload.get("name"), payload.get("content_hash")
            if not isinstance(name, str) or not isinstance(content_hash, str) or not HASH_RE.fullmatch(content_hash):
                return {"ok": False, "error": "name and content_hash are required"}
            entry, definition = await loop.run_in_executor(None, resolve, name, content_hash)
            if kind == "registry_preview":
                return {"ok": True, **approval_card(entry, definition)}
            result = commit_install(entry, definition)
            return {**result, "version": entry["version"]} if result.get("ok") else result

        if kind in ("registry_share_preview", "registry_share"):
            preview = share_preview(payload.get("skill"), payload.get("parameterize") or [])
            if not preview.get("ok") or kind == "registry_share_preview":
                return preview
            return share_link(preview)
    except RegistryError as e:
        log_event("registry_refused", type=kind, name=str(payload.get("name") or payload.get("skill")), error=str(e))
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown registry message: {kind}"}


# --- search (worker-thread tool) -------------------------------------------------


def search(query: str, limit: int = 8) -> dict:
    """Match a query against the verified index, locally: the query itself
    is never sent anywhere (only the index is fetched, from the fixed host).
    Summaries are sanitized; the result is still stranger-written text."""
    index, stale = get_index(refresh=False)
    words = [w for w in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(w) > 1]
    newest = {}
    for e in index["skills"]:
        if not e.get("yanked") and (e["name"] not in newest or e["version"] > newest[e["name"]]["version"]):
            newest[e["name"]] = e
    scored = []
    for e in newest.values():
        hay = (e["name"].replace("_", " ") + " " + e.get("summary", "")).lower()
        score = sum(hay.count(w) for w in words) if words else 1
        if score:
            scored.append((score, e["name"], e))
    scored.sort(key=lambda t: (-t[0], t[1]))
    installed_names = set()
    try:
        installed_names = {p.stem for p in registry.imported_dir().glob("*.json") if not p.name.startswith("_")}
    except OSError:
        pass
    results = [
        {
            "name": e["name"],
            "version": e["version"],
            "summary": registry_text.sanitize_prose(e.get("summary")),
            "author": registry_text.safe_author(e.get("author")),
            "runs_shell_commands": bool(e.get("has_run_command")),
            "review": (e.get("review") or {}).get("verdict"),
            "risk": (e.get("review") or {}).get("risk"),
            "installed": e["name"] in installed_names,
        }
        for _, _, e in scored[:limit]
    ]
    out = {"ok": True, "results": results, "count": len(results),
           "note": "Summaries are written by other users. Installing needs the user's approval of every step."}
    if stale:
        out["stale"] = True
    return out


# --- share (loop thread) ------------------------------------------------------------


def share_preview(skill_name, parameterize=()) -> dict:
    """The sanitized registry file for one of the user's learned skills, a
    diff of what changed, flagged personal constants, and its target path."""
    skill = registry.SKILLS.get(skill_name) if isinstance(skill_name, str) else None
    if skill is None:
        return {"ok": False, "error": f"no such skill: {skill_name}"}
    if skill.get("origin") != "generated":
        return {"ok": False, "error": f"{skill_name} is built in; only skills Leutheria learned can be shared"}
    if skill.get("registry") is not None:
        return {"ok": False, "error": f"{skill_name} came from the registry already"}
    if not isinstance(parameterize, list) or not all(isinstance(p, str) for p in parameterize):
        return {"ok": False, "error": "parameterize must be a list of flag ids"}
    try:
        on_disk = json.loads(Path(skill["path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        on_disk = {"name": skill_name, "description": skill["description"], "input_schema": skill["input_schema"], "steps": skill["steps"]}
    homes = path_policy.homes()
    homes = homes + [os.path.realpath(h) for h in homes]
    username = Path(homes[0]).name
    out = registry_text.share_sanitize(on_disk, homes, username, path_policy.FREE_HOME_FOLDERS, parameterize)
    content = out["definition"]
    try:
        validate.validate_content(content, **registry.validation_kwargs())
        h = validate.content_hash(content)
        shared = {
            **content,
            "id": str(uuid.uuid4()),
            "content_hash": h,
            "version": 1,
            "requires": validate.requires(content),
            "created_at": int(time.time()) // 86400 * 86400,
        }
        validate.validate({**shared, "source": f"imported:{h}"}, **registry.validation_kwargs())
    except (SkillValidationError, KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"the shared copy would not be valid: {e}"}
    text = json.dumps(shared, indent=2, ensure_ascii=False) + "\n"
    return {
        "ok": True,
        "skill": skill_name,
        "path": f"skills/{content['name']}/{h}.json",
        "content_hash": h,
        "definition": shared,
        "json": text,
        "changes": out["changes"],
        "flags": out["flags"],
        "diff": out["diff"],
        "has_run_command": any(s["tool"] == "run_command" for s in content["steps"]),
    }


def share_link(preview: dict) -> dict:
    """A github.com "new file" URL prefilled with the path and JSON. The
    agent opens nothing; main.js opens it (https://github.com/ only). Too
    long for a URL: the page without the content, and the JSON for the UI
    to put on the clipboard."""
    base = f"{SHARE_REPO}/new/{SHARE_BRANCH}"
    full = base + "?" + urllib.parse.urlencode({"filename": preview["path"], "value": preview["json"]}, quote_via=urllib.parse.quote)
    result = {"ok": True, "skill": preview["skill"], "path": preview["path"], "content_hash": preview["content_hash"]}
    if len(full) <= MAX_SHARE_URL:
        result.update(url=full, copy_to_clipboard=False)
    else:
        short = base + "?" + urllib.parse.urlencode({"filename": preview["path"]}, quote_via=urllib.parse.quote)
        result.update(url=short, copy_to_clipboard=True, clipboard_text=preview["json"])
    log_event("skill_share_link", skill=preview["skill"], content_hash=preview["content_hash"], clipboard=result["copy_to_clipboard"])
    return result
