"""Search the public shared-skill registry.

Read-only: fetches the signed index from the one fixed registry host
(core/registry_client.py) and matches the query against it on this
machine -- the query itself is never sent anywhere, which is why the
registry entry declares no "outbound" argument. The summaries are
written by other users, so the result is untrusted output.
"""


def search_shared_skills(query: str = "", limit: int = 8) -> dict:
    # Imported here: registry_client imports the skill registry, which
    # imports the tool registry, which imports this module.
    from agent.core import registry_client

    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 8
    try:
        return registry_client.search(query, limit)
    except registry_client.RegistryError as e:
        return {"ok": False, "error": str(e)}
