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
    def generate(self, messages: list, tools: list) -> LLMTurn:
        """Run one turn and return it.

        This is the seam future backends (Bedrock, a local model via Ollama)
        plug into -- callers only ever depend on this interface. Multi-turn
        tool loops live in the caller (agent/core/agent_loop.py), not here.
        """
