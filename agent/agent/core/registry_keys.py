"""Public keys the shared-skill registry's index may be signed with.

The registry's CI signs index.json with an Ed25519 private key that exists
only as the registry repository's REGISTRY_SIGNING_KEY secret. The client
(core/registry_client.py) accepts an index only if its signature verifies
against one of the keys below, matched by key_id. A list, not one key, so a
key can be rotated: ship the new key next to the old one, re-sign, then drop
the old one in a later release.

Each entry: {"key_id": "<short id, as written in index.json.sig>",
             "public_key": "<base64 of the 32 raw Ed25519 public key bytes>"}

DELIBERATELY EMPTY until the registry goes live. With no key configured the
client refuses to install, preview or list anything -- there is no
"unsigned" mode and no fallback. The lead adds the production public key
here at go-live (the private half never leaves the registry's CI).

The only other key the client ever accepts is a test key, given in the
environment (LEUTHERIA_REGISTRY_TEST_KEY) and honoured only together with
the test registry URL override (LEUTHERIA_REGISTRY_URL). Both are for the
check scripts and a local registry; see registry_client.py.
"""

PRODUCTION_KEYS: list = []
