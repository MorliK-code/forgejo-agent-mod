"""Инструменты агента (OpenAI function-calling) + их исполнение.

v1: файлы (read/write/edit), git (clone/commit/push), forgejo (comment/label),
    ssh_exec (хомелаб), honcho (search/conclude), skills (read).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


# ---------- schemas (OpenAI tools) ----------
def tool_schemas(ssh_hosts: list[str]) -> list[dict]:
    return [
        _t("read_file", "Прочитать файл (текст) из рабочей директории. relative path.",
           {"path": "string (относительный путь)"}),
        _t("write_file", "Записать (перезаписать) файл в рабочей директории.",
           {"path": "string", "content": "string"}),
        _t("edit_file", "Точечная замена: заменить old в файле на new (должно быть уникальным).",
           {"path": "string", "old": "string", "new": "string"}),
        _t("list_dir", "Список файлов/каталогов в рабочей директории.",
           {"path": "string (по умолчанию '.')"}),
        _t("run_shell", "Выполнить shell-команду в рабочей директории (локально).",
           {"command": "string", "timeout": "int (сек, дефолт 120)"}),
        _t("git_clone", "Клонировать git-репозиторий в рабочую директорию.",
           {"url": "string", "dir": "string (имя папки, дефолт из url)"}),
        _t("git_commit", "git add -A и commit в репозитории dir.",
           {"dir": "string", "message": "string"}),
        _t("git_push", "git push origin <branch> в dir.",
           {"dir": "string", "branch": "string (дефолт main)"}),
        _t("forgejo_comment", "Поставить комментарий в issue (отчёт/вопрос).",
           {"number": "int", "body": "string"}),
        _t("forgejo_set_label", "Поставить label на issue.",
           {"number": "int", "label": "string (agent|in-progress|review|done)"}),
        _t("ssh_exec", "Выполнить команду по SSH на хосте хомелаба.",
           {"host": f"string (один из: {', '.join(ssh_hosts)})",
            "command": "string", "timeout": "int (сек, дефолт 60)"}),
        _t("honcho_search", "Поиск в долговременной памяти (honcho) по тексту.",
           {"query": "string"}),
        _t("honcho_conclude", "Записать долгий факт/вывод в память (honcho).",
           {"content": "string"}),
        _t("skill_list", "Список доступных скиллов (имя + описание).", {}),
        _t("skill_read", "Прочитать SKILL.md скилла (по имени).",
           {"name": "string"}),
    ]


_TYPE_FIX = {"int": "integer", "float": "number", "string": "string", "boolean": "boolean"}


def _t(name: str, desc: str, props: dict) -> dict:
    props_out = {}
    for k, v in props.items():
        t = v.split(" ")[0].lower()
        props_out[k] = {"type": _TYPE_FIX.get(t, "string"), "description": v}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props_out},
        },
    }


# ---------- runtime ----------
class Tools:
    def __init__(self, workdir: str, forgejo, honcho, skills_dir: str,
                 ssh_hosts: dict, task_number: int):
        self.wd = Path(workdir).resolve()
        self.wd.mkdir(parents=True, exist_ok=True)
        self.fj = forgejo
        self.honcho = honcho
        self.skills = Path(skills_dir) if skills_dir else None
        self.ssh_hosts = ssh_hosts
        self.task = task_number
        self.owner = forgejo_owner if (forgejo_owner := getattr(forgejo, "owner", None)) else "morlik"
        self.repo = getattr(forgejo, "repo", "Forgejo-agents-tasks")

    # --- path guard: только внутри workdir ---
    def _safe(self, rel: str) -> Path:
        p = (self.wd / rel).resolve()
        if not (str(p) == str(self.wd) or str(p).startswith(str(self.wd) + os.sep)):
            raise PermissionError(f"path outside workdir: {rel}")
        return p

    def dispatch(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return str(handler(**args))
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    # --- files ---
    def _t_read_file(self, path: str) -> str:
        return self._safe(path).read_text(encoding="utf-8", errors="replace")

    def _t_write_file(self, path: str, content: str) -> str:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"written {len(content)} chars -> {path}"

    def _t_edit_file(self, path: str, old: str, new: str) -> str:
        p = self._safe(path)
        text = p.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return f"ERROR: '{old[:40]}...' found {text.count(old)} times (need exactly 1)"
        p.write_text(text.replace(old, new), encoding="utf-8")
        return "edited"

    def _t_list_dir(self, path: str = ".") -> str:
        base = self._safe(path)
        if not base.exists():
            return f"no such dir: {path}"
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in base.iterdir()))

    # --- shell / git ---
    def _t_run_shell(self, command: str, timeout: int = 120) -> str:
        r = subprocess.run(command, shell=True, cwd=str(self.wd), capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or "") + ("\n[stderr]\n" + r.stderr if r.stderr else "")
        return (out[-8000:] or f"exit {r.returncode}") + f"\n[exit {r.returncode}]"

    def _t_git_clone(self, url: str, dir: str = "") -> str:
        d = dir or url.rstrip("/").split("/")[-1].replace(".git", "")
        r = subprocess.run(["git", "clone", url, d], cwd=str(self.wd),
                           capture_output=True, text=True, timeout=300)
        return (r.stderr or r.stdout).strip()[-2000:] or f"cloned -> {d}"

    def _t_git_commit(self, dir: str, message: str) -> str:
        d = self._safe(dir)
        r1 = subprocess.run(["git", "add", "-A"], cwd=d, capture_output=True, text=True)
        r2 = subprocess.run(["git", "-c", "user.email=agent@local", "-c", "user.name=agent-loop",
                             "commit", "-m", message], cwd=d, capture_output=True, text=True)
        return (r2.stdout or r2.stderr).strip()[-2000:] or "nothing to commit"

    def _t_git_push(self, dir: str, branch: str = "main") -> str:
        d = self._safe(dir)
        r = subprocess.run(["git", "push", "origin", branch], cwd=d,
                           capture_output=True, text=True, timeout=120)
        return (r.stderr or r.stdout).strip()[-2000:] or f"pushed {branch}"

    # --- forgejo ---
    def _t_forgejo_comment(self, number: int, body: str) -> str:
        self.fj.create_comment(self.owner, self.repo, int(number), body)
        return f"comment posted on #{number}"

    def _t_forgejo_set_label(self, number: int, label: str) -> str:
        self.fj.add_labels(self.owner, self.repo, int(number), [label])
        return f"label '{label}' set on #{number}"

    # --- ssh ---
    def _t_ssh_exec(self, host: str, command: str, timeout: int = 60) -> str:
        ip = self.ssh_hosts.get(host)
        if not ip:
            return f"ERROR: unknown ssh host '{host}' (known: {', '.join(self.ssh_hosts)})"
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            f"root@{ip}", command],
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + ("\n[stderr]\n" + r.stderr if r.stderr else "")
        return (out[-8000:] or f"exit {r.returncode}") + f"\n[exit {r.returncode}]"

    # --- honcho ---
    def _t_honcho_search(self, query: str) -> str:
        return self.honcho.search(query, limit=8) or "(no matches)"

    def _t_honcho_conclude(self, content: str) -> str:
        self.honcho.conclude(content)
        return "conclusion saved"

    # --- skills ---
    def _t_skill_list(self) -> str:
        if not self.skills or not self.skills.exists():
            return "(no skills dir)"
        out = []
        for d in sorted(self.skills.iterdir()):
            if d.is_dir():
                skill_md = d / "SKILL.md"
                desc = ""
                if skill_md.exists():
                    m = re.search(r"^description:\s*(.+)$", skill_md.read_text(encoding="utf-8",
                                                                                errors="replace"), re.M)
                    desc = m.group(1).strip() if m else ""
                out.append(f"- {d.name}: {desc}")
        return "\n".join(out) or "(empty)"

    def _t_skill_read(self, name: str) -> str:
        p = (self.skills / name / "SKILL.md").resolve()
        if not str(p).startswith(str(self.skills.resolve())):
            return "ERROR: bad skill name"
        if not p.exists():
            return f"ERROR: skill '{name}' not found"
        return p.read_text(encoding="utf-8", errors="replace")
