"""Sign index.json: Ed25519 over its exact bytes.

    sign_index.py --index index.json --out index.json.sig [--key-file <pem>]

The key comes from --key-file or, in CI, the REGISTRY_SIGNING_KEY
environment variable (the PEM text keygen.py wrote). Writes
{"key_id": "<16 hex>", "sig": "<base64>"}.
"""

import argparse
import base64
import os
import sys
from pathlib import Path

import common


def sign(index_bytes: bytes, pem: bytes) -> dict:
    key = common.load_private_key(pem)
    return {"key_id": common.public_entry(key)["key_id"],
            "sig": base64.b64encode(key.sign(index_bytes)).decode("ascii")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign the registry index")
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    if args.key_file:
        pem = args.key_file.read_bytes()
    else:
        pem = os.environ.get("REGISTRY_SIGNING_KEY", "").encode("ascii")
        if not pem.strip():
            print("FAIL  no key: pass --key-file or set REGISTRY_SIGNING_KEY", file=sys.stderr)
            return 1
    try:
        doc = sign(args.index.read_bytes(), pem)
    except (common.RegistryError, ValueError, TypeError) as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 1
    common.write_json(args.out, doc)
    print(f"signed {args.index} with {doc['key_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
