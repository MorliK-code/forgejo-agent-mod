"""Агент-луп: сборка промпта (задача + история + honcho + скиллы) и цикл LLM ↔ tools.

После завершения — отчёт комментарием в issue + label.
"""
from __future__ import annotations

import logging
import os
import re
import json
from pathlib import Path

from . import honcho_client, llm as llm_mod, tools as tools_mod
from .forgejo_client import ForgejoError

log = logging.getLogger("agent")

SYSTEM_PROMPT = """Ты — автономный агент-разработчик. Работаешь по задачам из Forgejo.

Правила:
- Рабочая директория: {workdir}. Все файлы — только там.
- Исполняй задачу целиком: код, коммиты (git_commit + git_push), отчёт.
- Коммуникация с пользователем — ТОЛЬКО инструментом forgejo_comment (issue #{task}).
- Если нужен ответ пользователя — forgejo_comment с вопросом и заверши задачу (label review).
- Перед стартом: skill_list, при релевантности — skill_read (скиллы — статические инструкции).
- Память: honcho_search при необходимости; honcho_conclude — для долговременных фактов.
- Результат: краткий отчёт комментарием: что сделано, что проверено, что осталось.
- Финальный ответ: одно слово DONE (если всё сделано) или QUESTION (если ждёшь ответа).
"""

DONE_RE = re.compile(r"\bDONE\b")
QUESTION_RE = re.compile(r"\bQUESTION\b")


def pick_skills(skills_dir: str, task_title: str, task_body: str, max_n: int) -> str:
    """Наивный отбор релевантных скиллов по пересечению слов. Возвращает текст SKILL.md'ов."""
    d = Path(skills_dir)
    if not d or not d.exists():
        return ""
    words = set(re.findall(r"[a-zа-яё]{4,}", (task_title + " " + task_body).lower()))
    scored = []
    for sd in d.iterdir():
        if not sd.is_dir():
            continue
        sm = sd / "SKILL.md"
        if not sm.exists():
            continue
        head = sm.read_text(encoding="utf-8", errors="replace")[:3000]
        head_words = set(re.findall(r"[a-zа-яё]{4,}", head.lower()))
        score = len(words & head_words)
        scored.append((score, sd.name, head))
    scored.sort(key=lambda x: -x[0])
    picked = [s for s in scored[:max_n] if s[0] > 0]
    return "\n\n---\n\n".join(f"# SKILL: {n}\n{h}" for _, n, h in picked)


def pick_model_from_body(body: str, providers: list[str], default: str) -> str:
    """Модель задаётся дропдауном «Модель» в шаблоне → в body блок '### Модель'.
    Возвращает имя провайдера из providers, иначе default."""
    m = re.search(r"###\s*Модель\s*\n+(.+)", body)
    if m:
        val = m.group(1).strip().splitlines()[0].strip().strip("`")
        if val in providers:
            return val
        # допуск: значение совпало с provider частично
        for p in providers:
            if p.lower() in val.lower():
                return p
    return default


def effective_models(cfg) -> tuple[dict, str]:
    """Модели: models.json (панель управления) поверх config.yaml.
    models.json читается на каждую задачу — изменения без рестарта."""
    providers = dict(cfg.get("models", {}).get("providers", {}))
    default = cfg.get_path("models", "default") or "qwen-27b"
    mfile = Path(__file__).resolve().parent.parent / "models.json"
    if os.environ.get("AGENT_MODELS_FILE"):
        mfile = Path(os.environ["AGENT_MODELS_FILE"])
    if mfile.exists():
        try:
            from .config import expand
            d = expand(json.loads(mfile.read_text()))
            providers = {**providers, **(d.get("providers") or {})}
            if d.get("default"):
                default = d["default"]
        except Exception as e:
            log.warning("models.json unreadable: %s", e)
    return providers, default


def run_task(cfg, fj, honcho, task: dict) -> None:
    """Полный прогон одной задачи.

    Репо берётся из самой задачи (issue.repository), а не из конфига: агент-луп
    опрашивает и рабочий репо, и служебный туду-репо."""
    number = task["number"]
    title = task["title"]
    body = task.get("body", "")
    rep = task.get("repository") or {}
    owner = rep.get("owner") or cfg["forgejo"]["owner"]
    repo = rep.get("name") or cfg["forgejo"]["repo"]

    providers, default = effective_models(cfg)
    model_name = pick_model_from_body(body, list(providers.keys()), default)
    llm = llm_mod.make_from_config(providers, model_name)

    workdir = Path(cfg["agent"]["workdir"]) / f"task-{owner}-{repo}-{number}"
    workdir.mkdir(parents=True, exist_ok=True)
    ssh_hosts = cfg.get("ssh_hosts", {}) or {}
    tools = tools_mod.Tools(str(workdir), fj, honcho,
                            cfg.get_path("skills", "dir", default=""),
                            ssh_hosts, number)
    tools.owner = owner
    tools.repo = repo

    # --- контекст ---
    comments = fj.issue_comments(owner, repo, number)
    history = "\n".join(f"[{c.get('user', {}).get('login', '?')}] {c.get('body', '')}"
                        for c in comments[-20:])
    mem = ""
    if honcho:
        try:
            mem = honcho.context_block(title, cfg.get_path("honcho", "context_chars", default=6000))
        except Exception as e:
            log.warning("honcho context failed: %s", e)
    skills_text = pick_skills(cfg.get_path("skills", "dir", default=""), title, body,
                              cfg.get_path("skills", "max_in_prompt", default=3))

    system = SYSTEM_PROMPT.format(workdir=workdir, task=number)
    user = (
        f"## ЗАДАЧА #{number}: {title}\n\n{body}\n"
        + (f"\n## История комментариев\n{history}\n" if history else "")
        + (f"\n## ПАМЯТЬ (honcho)\n{mem}\n" if mem else "")
        + (f"\n## СКИЛЛЫ\n{skills_text}\n" if skills_text else "")
    )

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    schemas = tools_mod.tool_schemas(list(ssh_hosts.keys()))
    max_iter = cfg.get_path("agent", "max_iterations", default=25)

    final_content = ""
    for i in range(max_iter):
        msg = llm.chat(messages, tools=schemas)
        final_content = msg.get("content") or ""
        messages.append({"role": "assistant", "content": final_content,
                         **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
        if not msg.get("tool_calls"):
            break
        for tc in msg["tool_calls"]:
            fn = tc["function"]["name"]
            args = llm_mod.LLM.tool_call_args(tc)
            log.info("[%s] %s(%s)", number, fn, str(args)[:200])
            result = tools.dispatch(fn, args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:12000]})
    else:
        final_content += "\n(max iterations reached)"

    # --- отчёт ---
    report = (final_content or "(no final message)").strip()[:6000]
    has_question = bool(QUESTION_RE.search(final_content))
    fj.create_comment(owner, repo, number, f"**Агент** (model: {model_name})\n\n{report}")
    done_label = cfg.get_path("agent", "question_label", default="review") if has_question \
        else cfg.get_path("agent", "report_label", default="done")
    fj.remove_labels(owner, repo, number, ["in-progress"])
    # add_labels только ДОБАВЛЯЕТ (замена — set_labels) — держим todo/1..5/repo:<name>
    try:
        keep = [l for l in fj.issue_labels(owner, repo, number) if l != "in-progress"]
    except ForgejoError:
        keep = []
    fj.add_labels(owner, repo, number, keep + [done_label])
    log.info("[%s] finished -> %s", number, done_label)
