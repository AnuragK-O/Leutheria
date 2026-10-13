"""Does prompt caching of the tool list actually work? (calls the real model)

Two identical one-word requests through the real backend, seconds apart, with
the real tool schemas and system prompt. The first should WRITE the tool list
to the cache (cache_creation_input_tokens ~ the tool list's size); the second
should READ it (cache_read_input_tokens ~ the same). A second call that reads
nothing means something in the prompt prefix changes between calls -- a silent
miss that costs the full ~$0.11 per call with no error anywhere.

COSTS MONEY: two model calls -- roughly $0.15 the first time (a cache write
is 1.25x), a few cents if the cache is already warm. Needs ANTHROPIC_API_KEY
in the repo-root .env and credit on the account.

    cd agent && ./.venv/bin/python scripts/llm_cache_check.py

Run it after any change to how the prompt is assembled: the tool or skill
registries' ordering, anthropic_*_schemas(), llm_anthropic.py.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

# A worktree has no .env of its own; walk up to the first checkout that does.
for parent in [ROOT, *ROOT.parents]:
    if (parent / ".env").exists():
        load_dotenv(parent / ".env")
        break

from agent.core import agent_loop, llm_anthropic  # noqa: E402
from agent.skills.registry import anthropic_skill_schemas  # noqa: E402
from agent.tools.registry import anthropic_tool_schemas  # noqa: E402


def main() -> int:
    usages = []
    real_log = llm_anthropic.log_event
    # Capture the backend's own llm_usage record instead of writing two test
    # entries into the real events.jsonl.
    llm_anthropic.log_event = lambda event, **fields: usages.append(fields) if event == "llm_usage" else None
    try:
        backend = llm_anthropic.AnthropicBackend()
        for _ in range(2):
            tools = anthropic_tool_schemas() + anthropic_skill_schemas()
            backend.generate([{"role": "user", "content": "Say the single word: ready"}], tools, agent_loop._system_prompt())
    finally:
        llm_anthropic.log_event = real_log

    first, second = usages
    for label, usage in (("first ", first), ("second", second)):
        print(
            f"{label} call: input={usage['input_tokens']} "
            f"cache_write={usage['cache_creation_input_tokens']} cache_read={usage['cache_read_input_tokens']}"
        )

    failures = []
    wrote = (first["cache_creation_input_tokens"] or 0) + (first["cache_read_input_tokens"] or 0)
    if wrote < 5000:
        failures.append("first call neither wrote nor read a cached tool list (is the marker being sent?)")
    read = second["cache_read_input_tokens"] or 0
    if read < 5000:
        failures.append("second call read nothing from the cache: the prompt prefix differs between calls")
    if second["input_tokens"] > 5000:
        failures.append("second call still paid full price for most of the prompt")

    for failure in failures:
        print(f"FAIL  {failure}")
    if not failures:
        saved = read * 0.9  # reads bill at about a tenth of the input price
        print(f"ok    second call read {read} tokens from cache (~{saved / (read + second['input_tokens']):.0%} of its input cost saved)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
