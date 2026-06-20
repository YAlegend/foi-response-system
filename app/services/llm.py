"""Pluggable LLM provider.

Default is a deterministic "stub" so the system runs fully offline and tests are
reproducible. To use a real model, set FOI_LLM_PROVIDER and implement the call
in `_AnthropicProvider` / `_OpenAIProvider` (left as clearly-marked stubs so a
developer — or Claude Code — can wire them up without restructuring anything).
"""
from __future__ import annotations

from ..config import get_settings


class LLMProvider:
    def complete(self, system: str, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class _StubProvider(LLMProvider):
    """Echoes a concise deterministic completion. Used for offline/dev/test."""

    def complete(self, system: str, prompt: str) -> str:
        # The drafting service does not rely on free-form generation in stub mode;
        # it uses the structured template. This is only used for optional summaries.
        return "(stub LLM) " + prompt.strip().splitlines()[0][:200]


class _OllamaProvider(LLMProvider):  # pragma: no cover - needs a running daemon
    """Local on-prem model served by Ollama. Nothing leaves the council network —
    the data-sovereignty posture for the government client. Requires Ollama
    running and the model pulled (`ollama pull <model>`)."""

    def __init__(self, model: str, base_url: str, max_tokens: int):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens

    def complete(self, system: str, prompt: str) -> str:
        import httpx  # lazy
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            # Low temperature: grounded, deterministic drafting, not free invention.
            "options": {"temperature": 0.1, "num_predict": self.max_tokens},
        }
        resp = httpx.post(f"{self.base_url}/api/generate", json=payload, timeout=180)
        resp.raise_for_status()
        return (resp.json().get("response") or "").strip()


class _AnthropicProvider(LLMProvider):  # pragma: no cover - needs network/key
    def __init__(self, model: str, api_key: str):
        self.model, self.api_key = model, api_key

    def complete(self, system: str, prompt: str) -> str:
        from anthropic import Anthropic  # lazy optional dep
        client = Anthropic(api_key=self.api_key)
        msg = client.messages.create(
            model=self.model, system=system, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text


def is_enabled() -> bool:
    """True when a real generative model is configured (not the offline stub).
    Drafting uses this to decide between LLM synthesis and template assembly."""
    return get_settings().llm_provider.lower() not in ("", "stub")


def get_llm() -> LLMProvider:
    s = get_settings()
    provider = s.llm_provider.lower()
    if provider == "ollama":
        return _OllamaProvider(s.llm_model, s.ollama_base_url, s.llm_max_tokens)
    if provider == "anthropic":
        return _AnthropicProvider(s.llm_model, s.llm_api_key)
    return _StubProvider()
