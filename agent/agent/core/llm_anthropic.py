import anthropic

from agent.core.llm import LLMBackend, LLMTurn, ToolCall
from agent.core.logging_util import log_event

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 16000


def _with_cache_breakpoint(tools: list) -> list:
    """The tool list with a prompt-cache breakpoint on its last entry.

    The tool definitions are ~21k of the ~22.7k input tokens of every call
    (91 capabilities), identical from one call to the next, and they render
    first in the prompt (tools -> system -> messages). Marking the last tool
    caches all of them: later calls within the cache lifetime read that prefix
    at about a tenth of the input price instead of paying for it in full
    (measured 2026-10-13: ~$0.11 per call uncached, 2-3 calls per request).

    The breakpoint is on the tools, not the system prompt, on purpose: the
    system prompt ends in the per-request "Current session" note, so a marker
    there would rewrite the cache on every call. Anything that changes the
    tool list (a capability enabled or disabled, a skill learned) starts a new
    cache entry, which is correct -- keep the list's order deterministic.

    Default 5-minute lifetime: one spoken request is 2-3 calls seconds apart,
    which already pays for the write; a read refreshes the timer for free.

    Returns a new list -- the registry's own dicts are never mutated.
    """
    if not tools:
        return tools
    return [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]


class AnthropicBackend(LLMBackend):
    def __init__(self):
        self._client = anthropic.Anthropic()

    def generate(self, messages: list, tools: list, system: str = None) -> LLMTurn:
        kwargs = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "tools": _with_cache_breakpoint(tools),
            "messages": messages,
            # Effort is the cost/quality dial on current models (thinking is
            # adaptive and on by default on Haiku 5.5; it can't be switched off
            # with a budget). "low" = fewer, more consolidated tool calls and
            # terse replies -- right for short spoken requests. Raise it if
            # multi-step requests start missing steps.
            "output_config": {"effort": "low"},
        }
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(**kwargs)

        # The usage fields are the only ground truth that caching works: a
        # change to how the prompt is assembled can silently turn every call
        # into a miss with no error anywhere. cache_read should be ~the tool
        # list's size on every call after the first in a burst.
        usage = response.usage
        log_event(
            "llm_usage",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
        )

        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in response.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in response.content if b.type == "text")

        return LLMTurn(content=response.content, tool_calls=tool_calls, text=text)
