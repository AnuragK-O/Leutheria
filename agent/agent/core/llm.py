from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMTurn:
    content: list  # raw content blocks, appended verbatim to message history
    tool_calls: list = field(default_factory=list)  # list[ToolCall]; empty means Claude is done
    text: str = ""


class LLMBackend(ABC):
    @abstractmethod
    def generate(self, messages: list, tools: list, system: str = None) -> LLMTurn:
        """Run one turn and return it.

        This is the seam future backends (Bedrock, a local model via Ollama)
        plug into -- callers only ever depend on this interface. Multi-turn
        tool loops live in the caller (agent/core/agent_loop.py), not here.

        `system` is optional and per-call, not baked into the backend: the
        conversational loop (agent_loop.py) passes SYSTEM_PROMPT.md's content
        so replies sound like Leutheria; one-off structured-output calls
        (e.g. skill_learning.py generating JSON) deliberately omit it, so
        persona/tone instructions never bleed into a call that must return
        nothing but valid JSON.
        """
