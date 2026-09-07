from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class LLMTurn:
    content: List[Any]  # raw content blocks / messages for provider history
    tool_calls: List[ToolCall] = field(default_factory=list)
    text: str = ""
    usage: Dict[str, int] = field(default_factory=dict)  # input_tokens, output_tokens


@dataclass
class ProviderConfig:
    provider: str = "anthropic"
    model: str = "claude-opus-5"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096


class LLMProvider(ABC):
    """Abstract base class for all LLM inference backends.
    
    Decouples the core agent loop and skill learning from specific model APIs.
    Implementations handle message serialization, tool schema adaptation, and
    response normalization so the agent loop works uniformly across cloud APIs
    and local endpoints (e.g. LM Studio, Ollama, MLX).
    """

    @abstractmethod
    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Optional[str] = None,
    ) -> LLMTurn:
        """Run one inference turn with provided messages, tools, and optional system prompt."""
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return provider identifier e.g. 'anthropic', 'openai_compatible'."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return active model identifier."""
        pass
