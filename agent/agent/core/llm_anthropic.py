import anthropic

from agent.core.llm import LLMBackend, LLMTurn, ToolCall

MODEL = "claude-opus-5"
MAX_TOKENS = 16000


class AnthropicBackend(LLMBackend):
    def __init__(self):
        self._client = anthropic.Anthropic()

    def generate(self, messages: list, tools: list) -> LLMTurn:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=messages,
        )

        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in response.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in response.content if b.type == "text")

        return LLMTurn(content=response.content, tool_calls=tool_calls, text=text)
