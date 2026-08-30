import anthropic

from agent.core.llm import LLMBackend, TextResponse, ToolCall

MODEL = "claude-opus-5"
MAX_TOKENS = 16000


class AnthropicBackend(LLMBackend):
    def __init__(self):
        self._client = anthropic.Anthropic()

    def generate(self, messages: list, tools: list):
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=messages,
        )

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is not None:
            return ToolCall(name=tool_use.name, args=tool_use.input)

        text = next((b.text for b in response.content if b.type == "text"), "")
        return TextResponse(text=text)
