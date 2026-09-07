import os
from typing import Any, Dict, List, Optional

from agent.providers.base import LLMProvider, LLMTurn, ProviderConfig, ToolCall

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000


class AnthropicProvider(LLMProvider):
    def __init__(self, config: Optional[ProviderConfig] = None):
        import anthropic

        self.config = config or ProviderConfig(provider="anthropic")
        api_key = self.config.api_key or os.environ.get("ANTHROPIC_API_KEY")
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if self.config.base_url:
            client_kwargs["base_url"] = self.config.base_url

        self._client = anthropic.Anthropic(**client_kwargs)
        self._model = self.config.model or DEFAULT_MODEL
        self._max_tokens = self.config.max_tokens or DEFAULT_MAX_TOKENS

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Optional[str] = None,
    ) -> LLMTurn:
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }
        if tools:
            # Anthropic expects tools with {name, description, input_schema}
            formatted_tools = []
            for t in tools:
                if "input_schema" in t:
                    formatted_tools.append(t)
                elif "parameters" in t:
                    # convert from OpenAI format if provided
                    formatted_tools.append({
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "input_schema": t.get("parameters", {}),
                    })
                else:
                    formatted_tools.append(t)
            kwargs["tools"] = formatted_tools

        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)

        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in response.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in response.content if b.type == "text")
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
            }

        return LLMTurn(content=response.content, tool_calls=tool_calls, text=text, usage=usage)
