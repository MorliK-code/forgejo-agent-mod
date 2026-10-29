"""Honcho-клиент: контекст (память) на входе, conclusions на выходе.

Пути по OpenAPI (проверено на 110.5:8000):
  POST /v3/workspaces/{ws}/peers/{peer}/search        {query, filters?, limit?}
  GET  /v3/workspaces/{ws}/peers/{peer}/card
  POST /v3/workspaces/{ws}/conclusions                {conclusions:[{content, observer_id, observed_id}]}
  GET  /v3/workspaces/{ws}/peers/{peer}/context
Auth: Bearer JWT (workspace-ключ, без exp).
"""
from __future__ import annotations

import requests


class HonchoError(RuntimeError):
    pass


class Honcho:
    def __init__(self, base_url: str, api_key: str, workspace: str,
                 peer: str, ai_peer: str, timeout: int = 180):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.ws = workspace
        self.peer = peer
        self.ai_peer = ai_peer
        self.timeout = timeout

    def _h(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def _path(self, suffix: str) -> str:
        return f"{self.base}/v3/workspaces/{self.ws}{suffix}"

    # --- контекст на входе ---
    def search(self, query: str, limit: int = 8) -> str:
        """Гибридный поиск по сообщениям пира. Возвращает текст-эксцерпты."""
        r = requests.post(self._path(f"/peers/{self.peer}/search"),
                          headers=self._h(),
                          json={"query": query, "limit": limit},
                          timeout=self.timeout)
        if r.status_code >= 400:
            raise HonchoError(f"search {r.status_code}: {r.text[:300]}")
        return self._format_search(r.json())

    def card(self) -> str:
        """Карточка пира (краткий профиль)."""
        r = requests.get(self._path(f"/peers/{self.peer}/card"),
                         headers=self._h(), timeout=self.timeout)
        if r.status_code >= 400:
            return ""
        data = r.json()
        if isinstance(data, dict):
            pc = data.get("peer_card")
            if isinstance(pc, list):
                return "\n".join(str(x) for x in pc)
            return str(data.get("card", data))
        return str(data)

    def _format_search(self, data) -> str:
        # data: list of message excerpts (или {results: [...]})
        items = data.get("results") if isinstance(data, dict) else data
        if not items:
            return ""
        lines = []
        for it in items:
            if isinstance(it, dict):
                text = it.get("content") or it.get("text") or it.get("excerpt") or str(it)
                peer = it.get("peer") or it.get("author") or ""
                lines.append(f"[{peer}] {text}" if peer else str(text))
            else:
                lines.append(str(it))
        return "\n".join(lines)

    # --- запись на выходе ---
    def conclude(self, content: str) -> None:
        """Сохранить вывод (длительный факт) в память пира."""
        payload = {"conclusions": [{
            "content": content,
            "observer_id": self.ai_peer,
            "observed_id": self.peer,
        }]}
        r = requests.post(self._path("/conclusions"), headers=self._h(),
                          json=payload, timeout=self.timeout)
        if r.status_code >= 400:
            raise HonchoError(f"conclude {r.status_code}: {r.text[:300]}")

    def context_block(self, task_title: str, limit_chars: int = 6000) -> str:
        """Собрать блок памяти для промпта: card + поиск по задаче."""
        parts = []
        try:
            card = self.card()
            if card.strip() and card.strip().lower() not in ("", "[]", "null"):
                parts.append(f"### Карточка пира (память)\n{card}")
        except Exception:
            pass
        try:
            found = self.search(task_title, limit=8)
            if found.strip():
                parts.append(f"### Поиски по задаче (память)\n{found}")
        except Exception:
            pass
        if not parts:
            return ""
        block = "\n\n".join(parts)
        return block[:limit_chars]
