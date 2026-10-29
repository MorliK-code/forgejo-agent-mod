"""LLM-клиент: любой OpenAI-совместимый endpoint. Чат + tool-calling."""
from __future__ import annotations

import json

import requests


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, name: str, base_url: str, model: str, api_key: str = "not-needed",
                 max_tokens: int = 4096, temperature: float = 0.3, timeout: int = 600):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None) -> dict:
        """Возвращает message-объект: {role, content, tool_calls?}."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        r = requests.post(self.base_url + "/chat/completions", headers=headers,
                          json=payload, timeout=self.timeout)
        if r.status_code >= 400:
            raise LLMError(f"{self.name}: {r.status_code} {r.text[:400]}")
        try:
            data = r.json()
            return data["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.name}: bad response: {r.text[:300]}") from e

    @staticmethod
    def tool_call_args(tc: dict) -> dict:
        raw = tc.get("function", {}).get("arguments", "{}")
        if not raw or raw == "null":
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}


def make_from_config(providers: dict, name: str) -> LLM:
    if name not in providers:
        raise LLMError(f"model '{name}' not in config.models.providers")
    p = providers[name]
    return LLM(name=name, base_url=p["base_url"], model=p["model"],
               api_key=p.get("api_key", "not-needed"),
               max_tokens=p.get("max_tokens", 4096),
               temperature=p.get("temperature", 0.3),
               timeout=p.get("timeout", 600))
