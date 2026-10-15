"""Shared helpers for the registry scripts. Stdlib only, except the
signature helpers, which import `cryptography` when called.

The skill format itself is not reimplemented here: it is Leutheria's own
agent/agent/skills/validate.py, imported from a checkout of Leutheria at the
pinned ref (LEUTHERIA_REF), so the registry and the app can't disagree.
"""

import base64
import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

REGISTRY_ROOT = Path(__file__).resolve().parent.parent
MAX_SKILL_BYTES = 64 * 1024
HASH_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")  # same as validate.NAME_RE; validate is authoritative
INDEX_FORMAT = 1
SUMMARY_MAX = 200
REVIEW_VERDICTS = ("auto_approved", "human_approved")
RISKS = ("low", "medium", "high")


class RegistryError(Exception):
    pass


# --- Leutheria's validator and catalog ----------------------------------------


def load_validate(leutheria_dir):
    """Import agent.skills.validate from a Leutheria checkout. It is
    stdlib-only by contract, so this works on Linux CI."""
    agent_dir = Path(leutheria_dir).resolve() / "agent"
    if not (agent_dir / "agent" / "skills" / "validate.py").is_file():
        raise RegistryError(f"no agent/agent/skills/validate.py under {leutheria_dir}")
    if str(agent_dir) not in sys.path:
        sys.path.insert(0, str(agent_dir))
    return importlib.import_module("agent.skills.validate")


def load_catalog(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_catalog_path(leutheria_dir) -> Path:
    return Path(leutheria_dir) / "agent" / "tool_catalog.json"


# --- strict JSON ---------------------------------------------------------------


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            # Two parsers may keep different copies of a duplicated key; the
            # reviewer and the installer must read the same skill.
            raise RegistryError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _no_constants(name):
    raise RegistryError(f"non-JSON constant {name}")


def parse_skill_bytes(data: bytes) -> dict:
    """Strict: UTF-8 without BOM, one JSON object, no duplicate keys, no
    NaN/Infinity, at most MAX_SKILL_BYTES."""
    if len(data) > MAX_SKILL_BYTES:
        raise RegistryError(f"file is {len(data)} bytes; the cap is {MAX_SKILL_BYTES}")
    if data.startswith(b"\xef\xbb\xbf"):
        raise RegistryError("file starts with a byte-order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RegistryError(f"not UTF-8: {e}") from None
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except ValueError as e:
        raise RegistryError(f"not valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise RegistryError("not a JSON object")
    return value


def split_skill_path(rel: str):
    """'skills/<name>/<hash>.json' -> (name, hash), or raise. Name syntax is
    checked by the caller through validate.check_name."""
    parts = rel.split("/")
    if len(parts) != 3 or parts[0] != "skills" or not parts[2].endswith(".json"):
        raise RegistryError(f"{rel!r} is not skills/<name>/<content_hash>.json")
    digest = parts[2][: -len(".json")]
    if not HASH_RE.fullmatch(digest):
        raise RegistryError(f"{rel!r}: file name must be the 64-hex content_hash")
    return parts[1], digest


def check_skill_file(validate, kwargs: dict, rel: str, data: bytes) -> dict:
    """Everything a published skill file must satisfy, in one place (used by
    check_submission, build_index and verify_index)."""
    name, digest = split_skill_path(rel)
    try:
        validate.check_name(name)
    except validate.SkillValidationError as e:
        raise RegistryError(str(e)) from None
    definition = parse_skill_bytes(data)
    try:
        validate.validate_shared(definition, **kwargs)
    except validate.SkillValidationError as e:
        raise RegistryError(f"{rel}: {e}") from None
    if definition["name"] != name:
        raise RegistryError(f"{rel}: skill name {definition['name']!r} does not match its folder")
    if definition["content_hash"] != digest:
        raise RegistryError(f"{rel}: content_hash does not match the file name")
    return definition


# --- summary -------------------------------------------------------------------

_URL_RE = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+", re.IGNORECASE)
_MARKUP_RE = re.compile(r"[`*#>\[\]<>|~@\\]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁠-⁩﻿]")


def summary_of(description: str) -> str:
    """At most 200 plain characters for Discover. Not a security boundary --
    the client builds what the model sees itself -- just tidy and bounded."""
    text = _CONTROL_RE.sub(" ", description)
    text = _URL_RE.sub("", text)
    text = _MARKUP_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) > SUMMARY_MAX:
        text = text[: SUMMARY_MAX - 3].rstrip() + "..."
    return text


def flag_text(finding) -> str:
    """One finding (screener {where, issue, severity}, or a static finding of
    whatever shape) as a short plain line."""
    if isinstance(finding, dict):
        severity = finding.get("severity")
        where = finding.get("where") or finding.get("rule") or finding.get("id")
        issue = finding.get("issue") or finding.get("message") or finding.get("detail") or ""
        text = " ".join(str(x) for x in (f"[{severity}]" if severity else None, f"{where}:" if where else None, issue) if x)
    else:
        text = str(finding)
    return summary_of(text)[:160]


# --- files ---------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path, default=None):
    path = Path(path)
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def render_index(index: dict) -> bytes:
    """The exact bytes that are signed. ASCII-only so no editor or transport
    can renormalise them."""
    return (json.dumps(index, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


# --- keys ----------------------------------------------------------------------


def key_id_of(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def load_private_key(pem: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RegistryError("signing key is not Ed25519")
    return key


def public_raw(private_key) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def public_entry(private_key) -> dict:
    raw = public_raw(private_key)
    return {"key_id": key_id_of(raw), "public_key": base64.b64encode(raw).decode("ascii")}


def verify_signature(index_bytes: bytes, sig_doc: dict, keys: list) -> str:
    """Return the key_id that verified, or raise. `keys` is a list of
    {key_id, public_key (base64 raw 32 bytes)}; the key_id in the signature
    picks the key, and must match that key's own digest."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(sig_doc, dict) or set(sig_doc) != {"key_id", "sig"}:
        raise RegistryError("signature file must be exactly {key_id, sig}")
    for entry in keys:
        if entry.get("key_id") != sig_doc["key_id"]:
            continue
        raw = base64.b64decode(entry["public_key"], validate=True)
        if key_id_of(raw) != entry["key_id"]:
            raise RegistryError(f"key {entry['key_id']} does not match its own public key")
        try:
            signature = base64.b64decode(sig_doc["sig"], validate=True)
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, index_bytes)
        except (InvalidSignature, ValueError):
            raise RegistryError("index signature does not verify") from None
        return entry["key_id"]
    raise RegistryError(f"unknown signing key {sig_doc['key_id']!r}")
