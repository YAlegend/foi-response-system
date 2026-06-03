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


class _AnthropicProvider(LLMProvider):  # pragma: no cover - needs network/key
    def __init__(self, model: str, api_key: str):
        self.model, self.api_key = model, api_key

    def complete(self, system: str, prompt: str) -> str:
        # TODO (Claude Code): wire the Anthropic SDK here, e.g.:
        #   from anthropic import Anthropic
        #   client = Anthropic(api_key=self.api_key)
        #   msg = client.messages.create(model=self.model, system=system,
        #           max_tokens=1500, messages=[{"role": "user", "content": prompt}])
        #   return msg.content[0].text
        raise NotImplementedError("Anthropic provider not configured")


def get_llm() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "anthropic":
        return _AnthropicProvider(s.llm_model, s.llm_api_key)
    return _StubProvider()
