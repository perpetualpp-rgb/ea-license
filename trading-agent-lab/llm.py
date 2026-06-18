"""Minimal OpenAI-compatible chat client for the trading agents (stdlib only).

This is the seam that lets each agent decide with a *real* LLM — Hermes via the
Nous Research inference API by default (https://nousresearch.com), or any other
OpenAI-compatible `/v1/chat/completions` endpoint by changing `base_url`.

Design goal: **fail soft.** A missing API key, disabled config, network error,
timeout, or malformed response all return ``None`` so the caller transparently
falls back to the deterministic indicator rules. That keeps the lab fully
runnable offline (and safe to demo) even with LLM support compiled in.

No third-party dependency: requests go through ``urllib`` from the stdlib.
The API key is read from an environment variable (never the repo).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Dict, List, Optional

# Defaults target the Nous Research Hermes inference API (OpenAI-compatible).
# Override any of these via the "llm" block in config.json.
DEFAULTS = {
    "enabled": True,
    "base_url": "https://inference-api.nousresearch.com/v1",
    "model": "Hermes-3-Llama-3.1-70B",
    "api_key_env": "NOUS_API_KEY",
    "temperature": 0.3,
    "max_tokens": 200,
    "timeout": 20,
}


class LLMClient:
    """Thin wrapper over an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, cfg: Optional[Dict] = None):
        c = {**DEFAULTS, **(cfg or {})}
        self.enabled = bool(c["enabled"])
        self.base_url = str(c["base_url"]).rstrip("/")
        self.model = str(c["model"])
        self.api_key_env = str(c["api_key_env"])
        self.api_key = os.environ.get(self.api_key_env, "").strip()
        self.temperature = float(c["temperature"])
        self.max_tokens = int(c["max_tokens"])
        self.timeout = float(c["timeout"])

    @property
    def available(self) -> bool:
        """Usable only when enabled *and* an API key is present in the env."""
        return self.enabled and bool(self.api_key)

    def status(self) -> str:
        """Human-readable one-liner for logs/CLI summaries."""
        if not self.enabled:
            return "LLM disabled (rule-based agents)"
        if not self.api_key:
            return f"LLM enabled but ${self.api_key_env} is unset -> rule fallback"
        return f"LLM on: {self.model} @ {self.base_url}"

    def chat(self, messages: List[Dict]) -> Optional[str]:
        """POST a chat-completion request; return assistant text or None on failure."""
        if not self.available:
            return None
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except Exception:
            # Network down, auth error, rate limit, bad JSON -> fall back to rules.
            return None
