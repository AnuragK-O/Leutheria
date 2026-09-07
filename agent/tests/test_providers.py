import unittest
from unittest.mock import MagicMock, patch

from agent.providers.base import LLMProvider, LLMTurn, ProviderConfig, ToolCall
from agent.providers.factory import get_provider, register_provider
from agent.providers.openai_provider import OpenAICompatibleProvider


class DummyProvider(LLMProvider):
    def __init__(self, config=None):
        self.config = config

    @property
    def provider_name(self) -> str:
        return "dummy"

    @property
    def model_name(self) -> str:
        return "dummy-model"

    def generate(self, messages, tools, system=None):
        return LLMTurn(
            content=[{"type": "text", "text": "dummy response"}],
            tool_calls=[ToolCall(id="call_123", name="test_tool", args={"foo": "bar"})],
            text="dummy response",
        )


class TestProviders(unittest.TestCase):
    def test_dummy_provider(self):
        p = DummyProvider()
        self.assertEqual(p.provider_name, "dummy")
        self.assertEqual(p.model_name, "dummy-model")
        turn = p.generate([], [])
        self.assertEqual(turn.text, "dummy response")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "test_tool")
        self.assertEqual(turn.tool_calls[0].args, {"foo": "bar"})

    def test_provider_registration(self):
        register_provider("dummy", DummyProvider)
        cfg = ProviderConfig(provider="dummy", model="dummy-model")
        provider = get_provider(force_reload=True, config=cfg)
        self.assertIsInstance(provider, DummyProvider)

    @patch("openai.OpenAI")
    def test_openai_compatible_provider_generate(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # Setup mock completion response
        mock_choice = MagicMock()
        mock_choice.message.content = "I will check the folder."
        mock_tool_call = MagicMock()
        mock_tool_call.id = "tc_1"
        mock_tool_call.function.name = "list_files"
        mock_tool_call.function.arguments = '{"path": "~/Desktop"}'
        mock_choice.message.tool_calls = [mock_tool_call]

        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.prompt_tokens = 15
        mock_resp.usage.completion_tokens = 25
        mock_client.chat.completions.create.return_value = mock_resp

        cfg = ProviderConfig(
            provider="openai_compatible",
            model="qwen-2.5-coder",
            base_url="http://127.0.0.1:1234/v1",
            api_key="none",
        )
        provider = OpenAICompatibleProvider(cfg)

        messages = [{"role": "user", "content": "list my desktop"}]
        tools = [{
            "name": "list_files",
            "description": "list directory files",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }]

        turn = provider.generate(messages, tools, system="You are Leutheria.")

        self.assertEqual(turn.text, "I will check the folder.")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "list_files")
        self.assertEqual(turn.tool_calls[0].args, {"path": "~/Desktop"})
        self.assertEqual(turn.usage["input_tokens"], 15)
        self.assertEqual(turn.usage["output_tokens"], 25)

        # Check call arguments to mock_client
        create_kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(create_kwargs["model"], "qwen-2.5-coder")
        self.assertEqual(create_kwargs["messages"][0]["role"], "system")
        self.assertEqual(create_kwargs["messages"][0]["content"], "You are Leutheria.")
        self.assertEqual(create_kwargs["tools"][0]["function"]["name"], "list_files")


if __name__ == "__main__":
    unittest.main()
