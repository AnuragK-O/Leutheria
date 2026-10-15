"""Generate a registry signing key pair (Ed25519).

    keygen.py --private-out <path> [--keys keys/public_keys.json]

Writes the private key as unencrypted PKCS#8 PEM, mode 0600, refusing to
overwrite. That file's contents are the REGISTRY_SIGNING_KEY secret; keep it
nowhere else (a password manager, then delete the file). Prints the public
entry {"key_id", "public_key"} -- public_key is the raw 32-byte key in
base64, key_id the first 16 hex chars of its sha256 -- and, with --keys,
appends it to that list. The same entry goes into the client's
agent/agent/core/registry_keys.py.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import common


def generate(private_out: Path) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    fd = os.open(private_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return common.public_entry(key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a registry signing key")
    parser.add_argument("--private-out", type=Path, required=True)
    parser.add_argument("--keys", type=Path)
    args = parser.parse_args()
    try:
        entry = generate(args.private_out)
    except FileExistsError:
        print(f"FAIL  {args.private_out} exists; not overwriting a key", file=sys.stderr)
        return 1
    if args.keys:
        doc = common.read_json(args.keys, default={"keys": []})
        doc["keys"].append(entry)
        common.write_json(args.keys, doc)
    print(json.dumps(entry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
