"""Минимальный REST-клиент Forgejo (16.x / Gitea API). Только то, что нужно лупу."""
from __future__ import annotations

import base64
import logging
import time

import requests

API = "/api/v1"
log = logging.getLogger("forgejo")

# Канонические цвета меток agent-loop (#19: приоритет 5 = самый важный/красный,
# 1 = низший/серый). Нужны клиенту, чтобы любая создаваемая метка выходила в каноне.
LABEL_COLORS = {
    "todo": "6ea8ea", "agent": "1f6feb", "done": "57ab5a", "review": "f2cc11",
    "in-progress": "8250df", "format": "a371f7", "backlog": "6ea8ea",
    "5": "d1242f", "4": "fb8500",
    "3": "f2cc11", "2": "57ab5a", "1": "8c959f",
}


class ForgejoError(RuntimeError):
    pass


class ForgejoClient:
    def __init__(self, url: str, user: str, token: str, timeout: int = 30):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self._auth = base64.b64encode(f"{user}:{token}".encode()).decode()

    def _req(self, method: str, path: str, **kw):
        headers = {"Authorization": f"Basic {self._auth}"}
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        r = requests.request(method, self.base + API + path, headers=headers,
                             timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ForgejoError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    # --- issues ---
    def issues(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        r = self._req("GET", f"/repos/{owner}/{repo}/issues",
                      params={"state": state, "limit": 50})
        return r.json()

    def issue(self, owner: str, repo: str, num: int) -> dict:
        return self._req("GET", f"/repos/{owner}/{repo}/issues/{num}").json()

    def issue_comments(self, owner: str, repo: str, num: int) -> list[dict]:
        return self._req("GET", f"/repos/{owner}/{repo}/issues/{num}/comments").json()

    def create_comment(self, owner: str, repo: str, num: int, body: str) -> dict:
        return self._req("POST", f"/repos/{owner}/{repo}/issues/{num}/comments",
                         json={"body": body}).json()

    def add_labels(self, owner: str, repo: str, num: int, labels: list[str]) -> None:
        """Добавить метки по имени. Forgejo принимает либо числовые ID, либо
        уже существующие имена (неизвестное имя молча игнорируется), поэтому
        имена резолвятся в ID — недостающие метки создаются."""
        ids = self.ensure_labels(owner, repo, labels)
        self._req("POST", f"/repos/{owner}/{repo}/issues/{num}/labels",
                  json={"labels": ids})

    def set_labels(self, owner: str, repo: str, num: int, labels: list[str]) -> None:
        """Полная замена меток. POST /labels только ДОБАВЛЯЕТ — замена это PUT."""
        ids = self.ensure_labels(owner, repo, labels)
        self._req("PUT", f"/repos/{owner}/{repo}/issues/{num}/labels",
                  json={"labels": ids})

    def remove_labels(self, owner: str, repo: str, num: int, labels: list[str]) -> None:
        for l in labels:
            try:
                self._req("DELETE", f"/repos/{owner}/{repo}/issues/{num}/labels/{l}")
            except ForgejoError:
                pass  # label уже снят — не критично

    def issue_labels(self, owner: str, repo: str, num: int) -> list[str]:
        return [l["name"] for l in self._req(
            "GET", f"/repos/{owner}/{repo}/issues/{num}/labels").json()]

    def ensure_labels(self, owner: str, repo: str,
                      names: list[str], colors: dict | None = None) -> list[int]:
        """ID меток репо: существующие берём, недостающие создаём.
        Если у существующей метки цвет не совпадает с каноническим — перекрашиваем
        (приоритеты переопределили: 5 = красный/важный, 1 = серый; правка — PATCH).

        CreateIssueOption.labels принимает ТОЛЬКО числовые ID (имена — 422),
        поэтому перед созданием issue метки резолвятся здесь."""
        colors = colors or LABEL_COLORS
        have = {l["name"]: l for l in
                self._req("GET", f"/repos/{owner}/{repo}/labels",
                          params={"limit": 100}).json()}
        ids = []
        for n in names:
            l = have.get(n)
            if l:
                want = colors.get(n)
                if want and l.get("color", "").lstrip("#").lower() != want.lower():
                    try:
                        self._req("PATCH", f"/repos/{owner}/{repo}/labels/{l['id']}",
                                  json={"color": want})
                    except ForgejoError as e:
                        log.warning("recolor %s/%s %s: %s", owner, repo, n, e)
                ids.append(l["id"])
                continue
            created = self._req("POST", f"/repos/{owner}/{repo}/labels",
                                json={"name": n, "color": colors.get(n, "6ea8ea")}).json()
            ids.append(created["id"])
        return ids

    def create_issue(self, owner: str, repo: str, title: str, body: str,
                     labels: list[str] | list[int] | None = None,
                     due_date: str | None = None,
                     assignees: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body, "labels": labels or []}
        if due_date:
            payload["due_date"] = due_date
        if assignees:
            payload["assignees"] = assignees
        return self._req("POST", f"/repos/{owner}/{repo}/issues", json=payload).json()

    # --- polling helper ---
    def open_issues_since(self, owner: str, repo: str, after_iso: str) -> list[dict]:
        out = []
        for i in self.issues(owner, repo, state="open"):
            if i.get("updated_at", "") > after_iso:
                out.append(i)
        return out
