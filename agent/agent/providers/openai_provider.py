import json
import os
from typing import Any, Dict, List, Optional

from agent.providers.base import LLMProvider, LLMTurn, ProviderConfig, ToolCall

DEFAULT_MODEL = "gpt-4o"


class OpenAICompatibleProvider(LLMProvider):
    """Provider for OpenAI and any OpenAI-compatible endpoint.
    
    Supports:
    - Official OpenAI API
    - Local servers: LM Studio (http://127.0.0.1:1234/v1), Ollama (http://127.0.0.1:11434/v1), MLX
    - Third-party endpoints (OpenRouter, Groq, Together, vLLM)
    """

    def __init__(self, config: Optional[ProviderConfig] = None):
        import openai

        self.config = config or ProviderConfig(provider="openai_compatible")
        api_key = self.config.api_key or os.environ.get("OPENAI_API_KEY", "not-needed")
        base_url = self.config.base_url or os.environ.get("OPENAI_BASE_URL")

        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client = openai.OpenAI(**client_kwargs)
        self._model = self.config.model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self._max_tokens = self.config.max_tokens

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self._model

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        formatted = []
        for t in tools:
            if t.get("type") == "function":
                formatted.append(t)
                continue

            name = t.get("name")
            description = t.get("description", "")
            parameters = t.get("input_schema") or t.get("parameters", {"type": "object", "properties": {}})
            formatted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            })
        return formatted

    def _convert_messages(self, messages: List[Dict[str, Any]], system: Optional[str] = None) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        if system:
            converted.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            # Handle Anthropic-style raw assistant content blocks
            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    b_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                    if b_type == "text":
                        text = getattr(block, "text", "") if hasattr(block, "text") else block.get("text", "")
                        text_parts.append(text)
                    elif b_type == "tool_use":
                        b_id = getattr(block, "id", "") if hasattr(block, "id") else block.get("id", "")
                        b_name = getattr(block, "name", "") if hasattr(block, "name") else block.get("name", "")
                        b_input = getattr(block, "input", {}) if hasattr(block, "input") else block.get("input", {})
                        tool_calls.append({
                            "id": b_id,
                            "type": "function",
                            "function": {
                                "name": b_name,
                                "arguments": json.dumps(b_input),
                            },
                        })
                asst_msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
                if tool_calls:
                    asst_msg["tool_calls"] = tool_calls
                converted.append(asst_msg)
                continue

            # Handle Anthropic-style user tool_result blocks
            if role == "user" and isinstance(content, list):
                is_tool_result_list = False
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        is_tool_result_list = True
                        converted.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": str(item.get("content", "")),
                        })
                if is_tool_result_list:
                    continue

            # Normal string / plain dict messages
            if isinstance(content, (str, type(None))):
                converted.append({"role": role, "content": content})
            else:
                converted.append({"role": role, "content": str(content)})

        return converted

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system: Optional[str] = None,
    ) -> LLMTurn:
        formatted_messages = self._convert_messages(messages, system)
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": formatted_messages,
        }
        if self._max_tokens:
            kwargs["max_tokens"] = self._max_tokens

        formatted_tools = self._convert_tools(tools)
        if formatted_tools:
            kwargs["tools"] = formatted_tools

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tool_calls: List[ToolCall] = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                fn = tc.function
                try:
                    args = json.loads(fn.arguments) if isinstance(fn.arguments, str) else fn.arguments
                except Exception:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=fn.name, args=args))

        text = msg.content or ""

        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "input_tokens": getattr(response.usage, "prompt_tokens", 0),
                "output_tokens": getattr(response.usage, "completion_tokens", 0),
            }

        # Build raw content blocks mimicking standard format for history
        content_blocks = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            content_blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})

        return LLMTurn(content=content_blocks, tool_calls=tool_calls, text=text, usage=usage)
