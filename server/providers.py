"""
One adapter per provider behind a common interface:

    call(system, messages) -> Reply

`system` is a SystemPrompt, kept in two parts so each provider can mark the
stable part as cacheable. `messages` is [{"role": "user"|"assistant", ...}].

Prompt caching, per provider:

  Anthropic  explicit. A cache_control breakpoint on each system block. The
             prefix block is identical on every turn of a conversation, so it
             is written once and read back at a fraction of the input rate.
  OpenAI     automatic for prompts over ~1024 tokens. Nothing to switch on,
             but the prefix has to be byte-identical and come first, which is
             why the lesson file is appended last. prompt_cache_key groups
             requests that share a prefix onto the same cache.
  Google     implicit for supported models, on the same stable-prefix rule.

The model id is never hardcoded. It comes from the environment.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .prompt import SystemPrompt

log = logging.getLogger("decoder")

TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ProviderError(RuntimeError):
    """An API call failed in a way the caller should surface, not retry."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Reply:
    text: str
    input_tokens: int          # every input token, however it was billed
    output_tokens: int
    cached_input_tokens: int   # read back from cache, billed at ~0.1x
    cache_write_tokens: int = 0  # written to cache, billed at ~1.25x

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def fresh_input_tokens(self) -> int:
        """Input billed at the full rate: neither read from nor written to cache."""
        return max(0, self.input_tokens - self.cached_input_tokens
                   - self.cache_write_tokens)


class Provider:
    name = ""
    env_key = ""

    def __init__(self, api_key: str, model: str, max_output_tokens: int,
                 cache_ttl: str = "5m", min_cacheable_chars: int = 4000):
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        # How long a cached block survives. "5m" is the API default; "1h"
        # costs more to write but is worth it when traffic is sparse, because
        # the alternative is paying the full price again on every cold start.
        self.cache_ttl = cache_ttl
        # A block too small to be worth a breakpoint is sent uncached.
        self.min_cacheable_chars = min_cacheable_chars

    async def call(self, system: SystemPrompt, messages: list[dict],
                   cache_key: str) -> Reply:
        raise NotImplementedError

    @staticmethod
    async def _post(url: str, headers: dict, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise ProviderError(f"{r.status_code}: {r.text[:400]}", r.status_code)
        try:
            return r.json()
        except json.JSONDecodeError:
            raise ProviderError("provider returned a non-JSON body")


class AnthropicProvider(Provider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"
    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    async def call(self, system, messages, cache_key) -> Reply:
        # Two blocks, each its own cache breakpoint. The prefix block is
        # unchanged for the life of the conversation; the lesson block appears
        # only after a verdict and does not disturb the prefix already cached.
        blocks = []
        for part in system.parts:
            block = {"type": "text", "text": part}
            # Decide the breakpoint per block rather than always writing. A
            # block only earns the write premium if it will be read again.
            if len(part) >= self.min_cacheable_chars:
                cc = {"type": "ephemeral"}
                if self.cache_ttl and self.cache_ttl != "5m":
                    cc["ttl"] = self.cache_ttl
                block["cache_control"] = cc
            blocks.append(block)

        body = await self._post(
            self.URL,
            {"x-api-key": self.api_key, "anthropic-version": self.VERSION},
            {
                "model": self.model,
                "max_tokens": self.max_output_tokens,
                "temperature": 1,
                "system": blocks,
                "messages": [{"role": m["role"], "content": m["content"]}
                             for m in messages],
            },
        )

        text = "".join(b.get("text", "") for b in body.get("content", [])
                       if b.get("type") == "text")
        if not text:
            raise ProviderError(f"empty reply (stop_reason="
                                f"{body.get('stop_reason')})")
        if body.get("stop_reason") == "max_tokens":
            # The reply was cut mid-sentence. It still reaches the woman, so do
            # not throw it away, but say so: the response shape puts her
            # positioning last, and that is the first thing a cut reply loses.
            log.warning("reply hit max_tokens (%d) and was truncated - raise "
                        "MAX_OUTPUT_TOKENS", self.max_output_tokens)
        u = body.get("usage", {})
        cached = u.get("cache_read_input_tokens", 0) or 0
        written = u.get("cache_creation_input_tokens", 0) or 0
        return Reply(
            text=text,
            # Anthropic reports fresh, written and read input separately.
            input_tokens=u.get("input_tokens", 0) + written + cached,
            output_tokens=u.get("output_tokens", 0),
            cached_input_tokens=cached,
            cache_write_tokens=written,
        )


class OpenAIProvider(Provider):
    name = "openai"
    env_key = "OPENAI_API_KEY"
    URL = "https://api.openai.com/v1/chat/completions"

    async def call(self, system, messages, cache_key) -> Reply:
        body = await self._post(
            self.URL,
            {"Authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model,
                "temperature": 1,
                "max_completion_tokens": self.max_output_tokens,
                # Groups requests sharing a prefix onto one cache. Keyed by
                # stage and whether the lesson tail is on, never by user.
                "prompt_cache_key": cache_key,
                "messages": [{"role": "system", "content": system.text},
                             *({"role": m["role"], "content": m["content"]}
                               for m in messages)],
            },
        )
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected response shape: "
                                f"{json.dumps(body)[:300]}")
        if not text:
            raise ProviderError("empty reply")
        u = body.get("usage", {})
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        return Reply(
            text=text,
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            cached_input_tokens=cached,
        )


class GoogleProvider(Provider):
    name = "google"
    env_key = "GOOGLE_API_KEY"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    async def call(self, system, messages, cache_key) -> Reply:
        model = self.model.removeprefix("models/")
        url = f"{self.BASE}/models/{model}:generateContent?key={self.api_key}"
        body = await self._post(url, {}, {
            "system_instruction": {"parts": [{"text": system.text}]},
            "contents": [
                {"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]}
                for m in messages
            ],
            "generationConfig": {
                "temperature": 1,
                "maxOutputTokens": self.max_output_tokens,
            },
        })

        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError(f"no candidates "
                                f"(promptFeedback={body.get('promptFeedback')})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if not text:
            raise ProviderError(f"empty reply "
                                f"(finishReason={candidates[0].get('finishReason')})")
        u = body.get("usageMetadata", {})
        return Reply(
            text=text,
            input_tokens=u.get("promptTokenCount", 0),
            output_tokens=u.get("candidatesTokenCount", 0),
            cached_input_tokens=u.get("cachedContentTokenCount", 0) or 0,
        )


BY_NAME = {p.name: p for p in (AnthropicProvider, OpenAIProvider, GoogleProvider)}


def build(name: str, env: dict, model: str, max_output_tokens: int) -> Provider:
    cls = BY_NAME.get(name.strip().lower())
    if cls is None:
        raise SystemExit(f"PROVIDER must be one of {sorted(BY_NAME)}, got {name!r}")
    key = (env.get(cls.env_key) or "").strip()
    if not key:
        raise SystemExit(f"{cls.env_key} is not set, but PROVIDER={cls.name}")
    return cls(key, model, max_output_tokens)
