import json
import os
from pathlib import Path
from typing import Dict, Optional

from agent.providers.anthropic_provider import AnthropicProvider
from agent.providers.base import LLMProvider, ProviderConfig
from agent.providers.openai_provider import OpenAICompatibleProvider

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "config.json"

_current_provider: Optional[LLMProvider] = None
_custom_providers: Dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    "ollama": OpenAICompatibleProvider,
    "lmstudio": OpenAICompatibleProvider,
}


def load_config() -> ProviderConfig:
    """Load configuration from config.json or environment variables."""
    cfg_data = {}
    if CONFIG_FILE.exists():
        try:
            cfg_data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            cfg_data = {}

    provider_name = cfg_data.get("provider") or os.environ.get("LLM_PROVIDER")
    if not provider_name:
        # Auto-detect based on available keys or endpoint
        if os.environ.get("OPENAI_BASE_URL") or (os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY")):
            provider_name = "openai_compatible"
        else:
            provider_name = "anthropic"

    model = cfg_data.get("model") or os.environ.get("LLM_MODEL")
    api_key = cfg_data.get("api_key") or os.environ.get("LLM_API_KEY")
    base_url = cfg_data.get("base_url") or os.environ.get("OPENAI_BASE_URL")
    
    # Specific defaults for local servers if named
    if provider_name == "ollama" and not base_url:
        base_url = "http://127.0.0.1:11434/v1"
    elif provider_name == "lmstudio" and not base_url:
        base_url = "http://127.0.0.1:1234/v1"

    return ProviderConfig(
        provider=provider_name,
        model=model or ("gpt-4o" if "openai" in provider_name or provider_name in ("ollama", "lmstudio") else "claude-opus-5"),
        api_key=api_key,
        base_url=base_url,
    )


def save_config(config: ProviderConfig) -> None:
    """Persist provider configuration to config.json."""
    data = {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
    }
    if config.api_key:
        data["api_key"] = config.api_key
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def get_provider(force_reload: bool = False, config: Optional[ProviderConfig] = None) -> LLMProvider:
    """Retrieve the singleton LLM provider instance."""
    global _current_provider
    if _current_provider is None or force_reload or config is not None:
        cfg = config or load_config()
        provider_cls = _custom_providers.get(cfg.provider.lower(), OpenAICompatibleProvider)
        _current_provider = provider_cls(cfg)
    return _current_provider


def register_provider(name: str, provider_cls: type) -> None:
    """Register an additional provider implementation class."""
    _custom_providers[name.lower()] = provider_cls
