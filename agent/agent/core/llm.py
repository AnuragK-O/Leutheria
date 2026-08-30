from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class TextResponse:
    text: str


class LLMBackend(ABC):
    @abstractmethod
    def generate(self, messages: list, tools: list):
        """Return a ToolCall or a TextResponse.

        This is the seam future backends (Bedrock, a local model via Ollama)
        plug into -- callers only ever depend on this interface.
        """
