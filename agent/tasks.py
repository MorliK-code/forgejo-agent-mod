"""Туду-задачи поверх Forgejo: один служебный репозиторий вместо «задач в репо».

Forgejo не умеет задачи без репозитория — API требует owner/repo, и переноса
задачи между репо в API нет. Поэтому туду-лист живёт в одном служебном репо
(forgejo.tasks_repo): UI показывает «Задачи» и никогда не просит выбрать
репозиторий. Привязка к настоящему репо — label `repo:<name>` + комментарий,
номер задачи не меняется.

Метки: `todo` (по умолчанию), `agent` (взять агенту — триггер agent-loop),
`1..5` (приоритет), `repo:<name>` (привязка), `done`/`review` (ставит агент).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .forgejo_client import ForgejoClient, ForgejoError, LABEL_COLORS

# Семантика приоритета (#19): 5 — самый важный, 1 — выполнять последним.
# Порядок кортежа = порядок в выпадающем списке панели (сверху вниз по важности).
PRIORITIES = ("5", "4", "3", "2", "1")
LABEL_TODO = "todo"
LABEL_AGENT = "agent"
LABEL_FORMAT = "format"
# Канбан-состояния (#categories): ровно одно на задаче. 'agent' — НЕ состояние,
# это «назначен агент». Состояния: backlog (запланировано), in-progress (в работе),
# questions (ждёт ответа), done (готово), pause (пауза). backlog служебный: ставит
# полкер/панель, wake снимает его и ставит in-progress.
LABEL_BACKLOG = "backlog"
STATE_LABELS = ("in-progress", "questions", "done", "pause")


def priority_of(labels) -> int:
    """Числовой приоритет задачи: 5 (высший) .. 1, 0 = без приоритета."""
    p = [int(x) for x in labels if x in PRIORITIES]
    return max(p) if p else 0


def normalize_priorities(labels: list[str]) -> list[str]:
    """Правило #17: у задачи один приоритет — из нескольких меток остаётся наивысший."""
    if sum(1 for x in labels if x in PRIORITIES) <= 1:
        return list(labels)
    best = str(priority_of(labels))
    return [x for x in labels if x not in PRIORITIES or x == best]


def target(cfg) -> tuple[str, str]:
    """(owner, repo) служебного репо задач из forgejo.tasks_repo ('owner/repo')."""
    f = cfg["forgejo"]
    raw = str(f.get("tasks_repo") or "").strip()
    if "/" in raw:
        o, r = raw.split("/", 1)
        return o.strip(), r.strip()
    return str(f["owner"]), (raw or str(f["repo"]))


def targets(cfg) -> list[tuple[str, str]]:
    """Все репо, которые опрашивает агент-луп: рабочий + туду-репо пользователей.

    `forgejo.tasks_repos` — список `owner/inbox` (по одному на пользователя);
    если его нет, используется одиночный `forgejo.tasks_repo`."""
    f = cfg["forgejo"]
    out = [(str(f["owner"]), str(f["repo"]))]
    raw = f.get("tasks_repos")
    items = [str(x) for x in raw] if isinstance(raw, list) else ([str(raw)] if raw else [])
    if not items:
        items = [f.get("tasks_repo") or ""]
    for it in items:
        it = it.strip()
        if "/" in it:
            o, r = it.split("/", 1)
            pair = (o.strip(), r.strip())
        elif it:
            pair = (str(f["owner"]), it)
        else:
            continue
        if pair[1] and pair not in out:
            out.append(pair)
    return out


def inbox_name(cfg) -> str:
    return str(cfg["forgejo"].get("tasks_inbox_name") or "inbox").strip() or "inbox"


def client(cfg) -> ForgejoClient:
    f = cfg["forgejo"]
    return ForgejoClient(str(f["url"]), str(f["user"]), str(f.get("token") or ""))


def assignees(cfg) -> list[str]:
    """Кому назначаются создаваемые задачи.

    `/issues` в Forgejo по умолчанию показывает created_by, поэтому задача,
    созданная токеном агента, видна владельцу туду-репо только во вкладке
    «назначенные». Назначение по умолчанию = владелец tasks_repo (forgejo.tasks_assignees)."""
    f = cfg["forgejo"]
    a = f.get("tasks_assignees")
    if a is None:
        owner, _ = target(cfg)
        return [owner]
    if isinstance(a, str):
        return [a] if a else []
    return [str(x) for x in a if str(x).strip()]


def config_view(cfg) -> dict:
    owner, repo = target(cfg)
    return {"repo": f"{owner}/{repo}", "priorities": list(PRIORITIES),
            "assignees": assignees(cfg),
            "inbox_name": inbox_name(cfg),
            "tasks_repos": [f"{o}/{r}" for o, r in targets(cfg)],
            "labels": {"todo": LABEL_TODO, "agent": LABEL_AGENT},
            "paused": bool(cfg["forgejo"].get("paused"))}


def _slim(i: dict) -> dict:
    return {"number": i.get("number"), "title": i.get("title"),
            "state": i.get("state"),
            "labels": [l.get("name") for l in (i.get("labels") or [])],
            "assignees": [a.get("username") for a in (i.get("assignees") or [])],
            "created": i.get("created_at"), "updated": i.get("updated_at"),
            "due": i.get("due_date"), "comments": i.get("comments"),
            "author": (i.get("user") or {}).get("username"),
            "url": i.get("html_url")}


def list_tasks(cfg, state: str = "open", limit: int = 100) -> dict:
    owner, repo = target(cfg)
    fj = client(cfg)
    items = fj.issues(owner, repo, state=state)
    return {"repo": f"{owner}/{repo}", "tasks": [_slim(i) for i in items[:limit]]}


def create_task(cfg, title: str, body: str = "", priority: str = "",
                agent: bool = False, due: str = "") -> dict:
    title = (title or "").strip()[:255]
    if not title:
        raise ForgejoError("пустой заголовок задачи")
    labels = []
    p = (priority or "").strip().lower()
    if p in PRIORITIES:
        labels.append(p)
    if agent:
        labels.extend([LABEL_AGENT, LABEL_BACKLOG])
    owner, repo = target(cfg)
    due_date = (due or "").strip() or None
    if due_date:
        due_date = f"{due_date}T12:00:00Z"
    fj = client(cfg)
    # Forgejo 16: labels в create issue — только числовые ID
    label_ids = fj.ensure_labels(owner, repo, labels, LABEL_COLORS)
    issue = fj.create_issue(owner, repo, title, body or "", label_ids,
                            due_date=due_date, assignees=assignees(cfg))
    return _slim(issue)


def queue_fmt_comment(cfg, repo_full: str, number: int, cid=None) -> dict:
    """Deprecated: форматирование комментариев исполняет admin_server потоком
    (_fmt_comment_worker). Оставлено как no-op для старых клиентов."""
    return {"ok": True}


def issue_labels_view(cfg, repo_full: str, number: int) -> dict:
    """Метки задачи через API (панель сидит от них, а не от DOM: в DOM-селекторах
    сидит ВСЁ список меток репо — seed был бы ложным)."""
    repo_full = (repo_full or "").strip().strip("/")
    if "/" not in repo_full:
        raise ForgejoError("repo_full должен быть owner/repo")
    o, r = repo_full.split("/", 1)
    return {"labels": client(cfg).issue_labels(o, r, int(number))}


def tag_task(cfg, repo_full: str, number: int, priority: str | None = None,
             agent: bool = False, due: str = "", format_=None,
             pause: bool | None = None) -> dict:
    """Синхронизировать метки/дедлайн задачи, созданной браузером (нативной формой).

    priority: '1'..'5' — заменить приоритет; '' — снять любой; None — не трогать.
    agent: True — навесить, False — снять, None — не трогать.
    format_: принимается для совместимости со старым кэшированным JS, игнорируется
    (форматирование теперь живёт у комментариев: fmt_comment/fmt_q).
    Репо не ограничивается inbox-ом: панель работает на форме в ЛЮБОМ репо.
    Эндпоинт закрыт сессией Forgejo."""
    repo_full = (repo_full or "").strip().strip("/")
    if "/" not in repo_full:
        raise ForgejoError("repo_full должен быть owner/repo")
    o, r = repo_full.split("/", 1)
    fj = client(cfg)
    cur = fj.issue_labels(o, r, number)
    new = list(cur)
    if priority is not None:
        new = [x for x in new if x not in PRIORITIES]
        p = str(priority).strip().lower()
        if p in PRIORITIES:
            new.append(p)
        elif p:
            raise ForgejoError(f"неприоритет: {p}")
    if agent is True and LABEL_AGENT not in new:
        new.append(LABEL_AGENT)
    elif agent is False:
        new = [x for x in new if x != LABEL_AGENT]
    if pause is True and "pause" not in new:          # None — не трогать (панель шлёт только изменения)
        new.append("pause")
    elif pause is False:
        new = [x for x in new if x != "pause"]
    # канбан-согласованность (#categories): agent без состояния -> backlog;
    # backlog живёт только пока нет ни одного состояния
    if LABEL_AGENT in new:
        if not any(x in STATE_LABELS for x in new) and LABEL_BACKLOG not in new:
            new.append(LABEL_BACKLOG)
        elif LABEL_BACKLOG in new and any(x in STATE_LABELS for x in new):
            new = [x for x in new if x != LABEL_BACKLOG]
    else:
        new = [x for x in new if x != LABEL_BACKLOG]
    # старые сборы: снять служебные метки, которые больше не используются
    new = [x for x in new
           if x not in (LABEL_FORMAT, LABEL_TODO, "review") and not x.startswith("repo:")]
    if new != cur:
        fj.set_labels(o, r, number, new)
    if (due or "").strip():
        fj._req("PATCH", f"/repos/{o}/{r}/issues/{int(number)}",
                json={"due_date": f"{due.strip()}T12:00:00Z"})
    return {"ok": True, "repo": repo_full, "number": int(number), "labels": new}


def attach_repo(cfg, number: int, repo_full: str) -> dict:
    """Привязка задачи к репо: label repo:<name> + комментарий. Номер не меняется."""
    repo_full = (repo_full or "").strip().strip("/")
    if "/" not in repo_full:
        raise ForgejoError("repo_full должен быть owner/repo")
    o, r = target(cfg)
    fj = client(cfg)
    label = f"repo:{repo_full}"
    cur = fj.issue_labels(o, r, number)
    if label not in cur:
        fj.add_labels(o, r, number, cur + [label])
    fj.create_comment(o, r, number, f"Задача привязана к репозиторию **{repo_full}**.")
    return {"ok": True, "number": number, "label": label, "repo": repo_full}


def set_state(cfg, number: int, closed: bool) -> dict:
    o, r = target(cfg)
    state = "closed" if closed else "open"
    client(cfg)._req("PATCH", f"/repos/{o}/{r}/issues/{int(number)}",
                     json={"state": state})
    return {"ok": True, "number": int(number), "state": state}


# ---------- глобальная пауза очереди = метки (#25) ----------
def _agent_issue(issue: dict, labels: list[str], trigger: list[str], checkbox: str) -> bool:
    """Задача агентская: метка из trigger_labels или снятый чекбокс «Агент»."""
    if any(l in trigger for l in labels):
        return True
    body = issue.get("body") or ""
    return re.search(rf"-\s*\[x]\s*\**{re.escape(checkbox)}", body, re.I) is not None


def set_global_pause(cfg, on: bool, fj=None) -> dict:
    """Пауза/ресюм всей очереди действием по меткам.

    on:  'pause' на все открытые агентские задачи. Поллер повторяет каждый цикл,
         поэтому новая задача с 'agent' получает паузу автоматически.
    off: 'pause' снимается со ВСЕХ открытых агентских задач — включая те, которые
         поставили агенты (правило заказчика 22.10: кнопка возвращает в очередь всех).
    Возвращает итог по меткам, реестр «кто что повесил» не ведём.
    """
    f = cfg["forgejo"]
    trigger = f.get("trigger_labels", [LABEL_AGENT])
    checkbox = f.get("checkbox", "Агент")
    fj = fj or client(cfg)
    todo = []
    agent_tasks = 0
    for owner, repo in targets(cfg):
        if not repo:
            continue
        for i in fj.issues(owner, repo, state="open"):
            labels = [l["name"] for l in (i.get("labels") or [])]
            if not _agent_issue(i, labels, trigger, checkbox):
                continue
            agent_tasks += 1
            has = "pause" in labels
            if (on and not has) or (not on and has):
                todo.append((owner, repo, i["number"]))

    def one(t):
        o, r, n = t
        if on:
            fj.add_labels(o, r, n, ["pause"])
            return 1
        cur = fj.issue_labels(o, r, n)
        new = [x for x in cur if x != "pause"]
        if LABEL_AGENT in new and not any(x in STATE_LABELS for x in new) \
                and LABEL_BACKLOG not in new:
            new.append(LABEL_BACKLOG)   # снятая пауза -> задача снова «запланировано»
        fj.set_labels(o, r, n, new)
        return 1

    # метки по всем задачам должны ставиться быстро: последовательные запросы к
    # Forgejo давали ~8с на клик кнопки
    with ThreadPoolExecutor(max_workers=4) as ex:
        changed = sum(ex.map(one, todo)) if todo else 0
    return {"paused": on, "changed": changed, "agent_tasks": agent_tasks}
