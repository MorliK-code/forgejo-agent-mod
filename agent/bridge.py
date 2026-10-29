"""Мост agent-loop → Hermes (110.4).

Примитив: headless `hermes chat -Q --oneshot` по ssh (ключ CT106→user@110.4).
Hermes там со всеми своими инструментами (homelab-mcp: forgejo-комментарии/метки).

Два прогона:
  format — Hermes переписывает описание; луп вставляет блок «Форматированный:»
           в тело issue (оригинал сохраняется дословно).
  work   — именованная сессия work-<key> на задачу: каждый wake получает
           дамп НОВЫХ комментариев заказчика; память диалога — в сессии Hermes.
"""
from __future__ import annotations

from pathlib import Path

import os

import json

import hashlib
import logging
import re
import shlex
import subprocess

log = logging.getLogger("bridge")

MARK = "Форматированный:"  # legacy: старые тела задач содержат этот блок — не трогать


class BusySkip(Exception):
    """Сессия уже занята живым прогоном — wake пропускается без счётчика падений."""

COMMENT_FMT_PROMPT = """Перепиши сообщение разработчика ясно, коротко и полно: сохрани смысл,
все факты, цифры и формулировки заказчика; ничего не добавляй от себя. Структурируй, если
сообщение связное/списком. Ответь только переписанным текстом, без предисловий и кавычек.

=== СООБЩЕНИЕ ===
{body}"""

FORMAT_PROMPT = """Перепиши описание задачи разработчика ясно и полно: цель, объём работ, \
критерии готовности. Сохрани все детали автора и смысл, ничего не выдумывай. \
Ответь только переписанным текстом, без предисловий и пояснений.

=== ОПИСАНИЕ ===
{body}"""

WORK_PROMPT = """Forgejo задача #{number} ({repo_full}). Ты — агент задачи; диалог с заказчиком \
ведётся ТОЛЬКО в комментариях задачи (инструмент mcp__homelab_mcp__comment, \
repo="{repo_full}", number={number}).

Порядок:
1) Прочитай задачу и все комментарии: mcp__homelab_mcp__show_issue(repo="{repo_full}", number={number}).
2) {wake_line}
3) Работай в рабочей директории сессии. Правила репозиториев:
   - В репо задач (inbox) коммитить ЗАПРЕЩЕНО.
   - Если заказчик комментарием сослался на репозиторий — работай в нём (клонируй \
http://192.168.110.9:8080, коммить и пушь git-ом — ключ доступа есть).
   - Если работа создала файлы и явного репо нет: создай отдельный репозиторий \
(mcp__homelab_mcp__create_repo, name 'task-<номер>', user '{owner}') и запушь туда. \
Ссылку на репо и коммит дай комментарием в задачу. Метку repo: НЕ вешать. Файлов нет — репозиторий не создавай.
4) Отчёт и вопросы заказчику — комментарием в задачу. Ровно один комментарий за wake: вопрос \
(если без ответа заказчика продолжить нельзя) ИЛИ финальный отчёт о выполненной работе. Никаких \
технических и служебных комментариев: падений, повторных прогонов, хода тестов, мета-объяснений — \
это только в служебный лог, не в задачу.
5) Никогда не закрывай задачу — закрытие (state=closed) исключительно право автора после проверки. \
Метки, разрешённые агенту: ТОЛЬКО 'in-progress', 'done', 'questions', 'pause'. Приоритеты ('1'..'5'), \
'agent' и любые прочие метки — клиента, агент их не трогает и свои приоритеты не добавляет; \
set_labels заменяет весь набор меток — копируй метки заказчика без изменений. 'in-progress': \
взялся за задачу — не снимай его; снимается только когда работа закончена: \
mcp__homelab_mcp__set_labels = все текущие метки заказчика (приоритет, 'agent') + 'done', \
без 'in-progress'. Если ждёшь ответ заказчика — вопрос комментарием, метка 'questions' и \
'in-progress' НЕ снимается. 'pause': ставь, когда задачу надо приостановить, при возврате \
к работе — сними и вернись к 'in-progress'. Метки 'review', 'todo', 'format', 'repo:...' не использовать.
6) Перед DONE — обязательна сквозная (end-to-end) проверка сделанного по скиллу e2e-bug-sweep: \
реальный прогон результата в живой системе, а не «на вид работает». Нет доказательств прогона — 'done' не ставить.
   Если в описании задачи есть чек-лист «Критерии готовности» (или аналогичный список пунктов): \
перед DONE сверь каждый пункт чек-листа и в финальном отчёте перечисли доказательства по каждому \
пункту; любой непроверенный пункт — 'done' не ставить.
7) Сделал задачу — ждать не надо: цикл сам берёт следующую задачу, если она есть. Новые комментарии \
читаются даже у done-задачи: если заказчик написал — продолжай работу (поллер на wake сам \
снимет 'done'/'questions' и вернёт 'in-progress').
8) Этапы по времени: если у задачи есть срок (due) — планируй работу этапами так, чтобы успеть к \
сроку: часть работы сделана и следующая нужна позже (ждать тест/деплой/прогресс) — заверши прогон \
последней строкой NEXT:<минуты> (минуты до следующего этапа; шаг ≈ остаток_до_срока / число \
оставшихся этапов). Полкер сам разбудит задачу на следующий этап — спать внутри прогона нельзя.
Последнее слово финального ответа: DONE | REVIEW | WAIT | NEXT:<минуты>."""


RUN_FILE = Path(os.environ.get("AGENT_RUN_FILE", "/opt/agent-loop/run.json"))


def _run(cfg, prompt, session=None, workdir=None, timeout=None):
    b = cfg.get("bridge") or {}
    host = str(b.get("host", "192.168.110.4"))
    user = str(b.get("user", "user"))
    bin_ = str(b.get("bin", "/home/user/.local/bin/hermes"))
    tmo = int(timeout or b.get("run_timeout", 1200))
    inner = "%s chat -Q --oneshot --query-file - --yolo --source tool" % bin_
    # shortcut: --max-turns/--run-budget нельзя с именованной сессией (Hermes хранит
    # policy создания); потолок — внешний `timeout`: переживает обрыв ssh (сирот нет)
    inner = "timeout %d %s" % (tmo, inner)
    if session:
        inner += " --create-if-missing -c %s" % shlex.quote(session)
    if workdir:
        # --in требует существующий каталог
        inner = "mkdir -p %s && %s --no-restore-cwd --in %s" % (
            shlex.quote(workdir), inner, shlex.quote(workdir))
    # flock -n: второй прогон по той же сессии не запускаем (сирота держит лок)
    if session:
        lock = "/home/user/agent-work/.%s.lock" % re.sub(r"[^A-Za-z0-9_.-]", "-", session)
        remote = "flock -n %s bash -c %s" % (shlex.quote(lock), shlex.quote(inner))
    else:
        remote = inner
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           f"{user}@{host}", remote]
    log.info("hermes run: session=%s timeout=%s", session, tmo)
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=tmo + 30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ssh run timeout (kill)")
    out = r.stdout or ""
    # -Q печатает в конце 'session_id: ...' — он не часть ответа
    out = "\n".join(l for l in out.splitlines() if not l.startswith("session_id:")).strip()
    if r.returncode == 1 and not out and session:
        raise BusySkip(f"сессия {session} уже занята прогоном")
    return r.returncode, out, (r.stderr or "").strip()


def original_part(body: str) -> str:
    return (body or "").split(MARK)[0].rstrip()


def body_hash(body: str) -> str:
    return hashlib.sha256(original_part(body).encode()).hexdigest()


def _hide_agent_sessions(cfg):
    """Сессии агента (work-*) не должны виднеться в чат-истории юзера: hidden=1.
    --source tool работает только без -c, поэтому правим БД после каждого прогона."""
    b = cfg.get("bridge") or {}
    host = str(b.get("host", "192.168.110.4"))
    user = str(b.get("user", "user"))
    script = ("import sqlite3;"
              "db=sqlite3.connect('/home/user/.hermes/state.db',timeout=5);"
              "db.execute('PRAGMA busy_timeout=5000');"
              "db.execute(\"update sessions set hidden=1 where "
              "(title like 'work-%' or created_source='tool') and hidden=0\");db.commit()")
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", f"{user}@{host}",
                        "python3 -c " + shlex.quote(script)], timeout=30)
    except Exception as e:
        log.info("hide agent sessions: %s", e)


def sweep_agent_runs(cfg) -> bool:
    """Пауза/чат-гейт: убить СИРОТ — прогоны, не указанные в run.json
    (их поллер умер/рестартнулся, наблюдатель их не видит). Маркер общий:
    каждый bridge-прогон стартует с workdir agent-work/<key>."""
    b = cfg.get("bridge") or {}
    host, user = str(b.get("host", "192.168.110.4")), str(b.get("user", "user"))
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                            f"{user}@{host}", "pkill -TERM -f " + shlex.quote("agent-work/")],
                           capture_output=True, timeout=20)
        return p.returncode == 0
    except Exception as e:
        log.info("sweep agent runs: %s", e)
        return False


def _parse_idle(raw):
    """epoch (float) или ISO -> секунды без активности; мусор/пусто -> None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    import time as _t
    from datetime import datetime
    try:
        ts = float(raw)
    except ValueError:
        try:
            ts = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return max(0.0, _t.time() - ts)


def main_chat_idle(cfg):
    """Секунды с последней активности ОСНОВНОГО чата Hermes (state.db на 110.4).
    Основной чат = любой НЕагентский диалог; активность = ЛЮБАЯ его реплика
    (user/assistant/tool): пока агент в чате печатает или думает — чат занят и
    задачи не будят (иначе wake попадает в середину диалога: метр только по
    user-репликам давал 'тишину' во время долгих ответов — баг 25.10).
    None при любой ошибке — гейт на неё не должен стопорить очередь."""
    b = cfg.get("bridge") or {}
    host, user = str(b.get("host", "192.168.110.4")), str(b.get("user", "user"))
    script = ("import sqlite3;"
              "db=sqlite3.connect('/home/user/.hermes/state.db',timeout=5);"
              "db.execute('PRAGMA busy_timeout=5000');"
              "r=db.execute(\"select max(m.timestamp) from messages m join sessions s "
              "on s.id=m.session_id "
              "where ifnull(s.title,'') not like 'work-%' "
              "and ifnull(s.created_source,'')<>'tool'\").fetchone();"
              "print('' if not r or r[0] is None else r[0])")
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=accept-new",
                            f"{user}@{host}", "python3 -c " + shlex.quote(script)],
                           capture_output=True, text=True, timeout=20)
        return _parse_idle((p.stdout or "").strip())
    except Exception:
        return None


def chat_gate_active(cfg) -> bool:
    """Чат-гейт: основной чат Hermes активен (< main_chat_idle_gate_s, default 180) —
    задачи не будить и живые прогоны стопать: чат = приоритет №1.
    Нет связи с 110.4 (None) — гейт НЕ активен: очередь важнее молчания."""
    gate_s = int((cfg.get("bridge") or {}).get("main_chat_idle_gate_s", 180))
    if gate_s <= 0:
        return False
    idle = main_chat_idle(cfg)
    return idle is not None and idle < gate_s


def work_session_idle(cfg, session, since_iso=""):
    """Секунды тишины work-сессии (messages сессии title=session в state.db на 110.4).
    Тишина = с max(последнее сообщение, старт прогона): свежий wake, ещё ничего не
    написавший, молчит от старта прогона (started/last_wake), а не от сообщений
    старого wake — иначе мгновенно упавший прогон ложно считался бы зависшим.
    None при любой ошибке/неизвестной сессии — fail-open, как метр чат-гейта."""
    b = cfg.get("bridge") or {}
    host, user = str(b.get("host", "192.168.110.4")), str(b.get("user", "user"))
    if not session or not re.fullmatch(r"[A-Za-z0-9_.-]+", session):
        return None
    script = ("import sqlite3;"
              "db=sqlite3.connect('/home/user/.hermes/state.db',timeout=5);"
              "db.execute('PRAGMA busy_timeout=5000');"
              "r=db.execute(\"select max(m.timestamp) from messages m join sessions s "
              "on s.id=m.session_id where s.title=%r\").fetchone();"
              "print('' if not r or r[0] is None else r[0])" % session)
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=accept-new",
                            f"{user}@{host}", "python3 -c " + shlex.quote(script)],
                           capture_output=True, text=True, timeout=20)
        last = _parse_idle((p.stdout or "").strip())
    except Exception:
        return None
    if last is None:
        return None
    since = _parse_idle(since_iso)
    return min(last, since) if since is not None else last


def watchdog_idle(cfg, session, since_iso=""):
    """Watchdog живого прогона (#28): тишина work-сессии выше порога
    (bridge.watchdog_idle_s, default 600) -> секунды тишины (наблюдатель убивает
    прогон, задача возвращается в очередь ran=False без fails++, как при чат-гейте);
    иначе None. Порог <=0 (выключен) или нет связи с 110.4 — None, не убивать."""
    t = int((cfg.get("bridge") or {}).get("watchdog_idle_s", 600))
    if t <= 0 or not session:
        return None
    idle = work_session_idle(cfg, session, since_iso)
    return idle if idle is not None and idle > t else None


# #28: hermes, убитый TERM'ом наблюдателя, успевает дописать финальное сообщение —
# тишина сессии на учёте wake уже не видна. Факт kill передаётся полкеру отметкой
# (наблюдатель пишет, полкер потребляет один раз).
STOP_MARK = RUN_FILE.with_name("watchdog-stop.json")


def mark_watchdog_stop(key: str):
    try:
        STOP_MARK.write_text(json.dumps({"key": key, "ts": _iso_now()}))
    except Exception:
        pass


def watchdog_stop_marked(key: str) -> bool:
    """True, если наблюдатель недавно убил прогон именно этой задачи (отметка
    действительна 15 мин); отметка потребляется. Иначе False."""
    try:
        from datetime import datetime, timezone
        d = json.loads(STOP_MARK.read_text())
        ts = datetime.fromisoformat(str(d.get("ts")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return False
    if str(d.get("key")) != str(key) or age > 900:
        return False
    try:
        STOP_MARK.unlink()
    except OSError:
        pass
    return True


def _fmt_text(cfg, body):
    """Прогн формататора по тексту; raises RuntimeError при сбое."""
    code, text, err = _run(cfg, FORMAT_PROMPT.format(body=body or ""),
                           timeout=int((cfg.get("bridge") or {}).get("format_timeout", 180)))
    if code != 0 or not text:
        raise RuntimeError(f"format body failed rc={code}: {err[:200] or text[:200]}")
    return text


def _strip_mark(body):
    """Отсечь блок «Форматированный: ...» (первое вхождение до конца тела)."""
    i = (body or "").find(MARK)
    return (body[:i].rstrip() if i != -1 else body or "")


def format_body(cfg, fj, owner, repo, number, body):
    """Форматирование описания задачи (запрошено при создании): оригинал сохраняется,
    ниже дописывается блок «Форматированный:». Повторно не оборачивает (MARK уже есть)."""
    if MARK in (body or ""):
        return False
    text = _fmt_text(cfg, body)
    new_body = (body or "").rstrip() + f"\n\n{MARK}\n\n{text}"
    fj._req("PATCH", f"/repos/{owner}/{repo}/issues/{int(number)}", json={"body": new_body})
    _hide_agent_sessions(cfg)
    return True


def restore_body(cfg, fj, owner, repo, number, body):
    """Снять форматирование с описания: вернуть оригинал (без блока MARK)."""
    stripped = _strip_mark(body)
    if stripped == (body or ""):
        return False
    fj._req("PATCH", f"/repos/{owner}/{repo}/issues/{int(number)}", json={"body": stripped})
    _hide_agent_sessions(cfg)
    return True


def reformat_body(cfg, fj, owner, repo, number, body):
    """Переформатировать описание: старый MARK-блок снимается, по оригиналу заново
    гоняется формататор, блок дописывается (защита от дублирования — не наслаивается)."""
    src = _strip_mark(body)
    if not src.strip():
        return False
    text = _fmt_text(cfg, src)
    new_body = src.rstrip() + f"\n\n{MARK}\n\n{text}"
    fj._req("PATCH", f"/repos/{owner}/{repo}/issues/{int(number)}", json={"body": new_body})
    _hide_agent_sessions(cfg)
    return True


def format_comment(cfg, fj, owner, repo, comment_id, body):
    """Переформатировать один комментарий (замена тела, оригинал не сохраняется —
    shortcut: если понадобится история правок, добавить details-блок)."""
    code, text, err = _run(cfg, COMMENT_FMT_PROMPT.format(body=body),
                           timeout=int((cfg.get("bridge") or {}).get("format_timeout", 180)))
    if code != 0 or not text:
        raise RuntimeError(f"format comment failed rc={code}: {err[:200] or text[:200]}")
    if text.strip() == (body or "").strip():
        return False
    fj._req("PATCH", f"/repos/{owner}/{repo}/issues/comments/{int(comment_id)}",
            json={"body": text})
    _hide_agent_sessions(cfg)
    return True


def session_name(owner, repo, number):
    safe = f"{owner}-{repo}-{number}".replace("/", "-")
    return f"work-{safe}"


def work_task(cfg, fj, owner, repo, number, body, new_comments, first, last_err=""):
    workdir = f"/home/user/agent-work/{owner}-{repo}-{number}"
    if first:
        wake_line = ("Первый заход: начни работу по описанию задачи "
                     "(учти блок «Форматированный:», если есть).")
    else:
        dump = "\n\n".join(
            f"[{c['user'].get('login', '?')} @ {c.get('created_at', '')}]\n{c.get('body', '')}"
            for c in new_comments)
        wake_line = "Новые комментарии заказчика (продолжай работу с учётом них):\n" + dump
    # #31: прошлый прогон упал — wake получает причину и не наступает на те же грабли
    if last_err:
        wake_line = (f"ВНИМАНИЕ: предыдущий прогон этой задачи упал с ошибкой: {last_err}. "
                     "Не повторяй этот путь — сначала разбери причину падения.\n\n") + wake_line
    prompt = WORK_PROMPT.format(number=number, repo_full=f"{owner}/{repo}",
                                wake_line=wake_line, owner=owner)
    RUN_FILE.write_text(json.dumps({"key": f"{owner}/{repo}#{number}", "owner": owner,
                                    "repo": repo, "num": number,
                                    "session": session_name(owner, repo, number),
                                    "started": _iso_now()}))  # #28: отсчёт тишины watchdog-а
    try:
        code, out, err = _run(cfg, prompt, session=session_name(owner, repo, number),
                              workdir=workdir)
    finally:
        RUN_FILE.unlink(missing_ok=True)
    # hermes -Q может выйти ненулевым после фактического завершения — маркер
    # финального ответа надёжнее кода возврата
    if code != 0 and not re.search(r"(\b(DONE|REVIEW|WAIT)\s*$)|NEXT:\s*\d+\s*$", out):
        _hide_agent_sessions(cfg)
        raise RuntimeError(f"hermes rc={code}: {(err or out)[:400]}")
    _hide_agent_sessions(cfg)
    return code, out


def new_user_comments(fj, owner, repo, number, last_cid, last_upd, agent_login):
    """Свежие или РЕДАКТИРОВАННЫЕ комментарии НЕ агента.

    Триггер: id > last_cid ИЛИ updated_at > last_upd (ISO-сравнение). Login —
    регистронезависимо: в API он 'Hermes'."""
    cs = fj.issue_comments(owner, repo, number) or []
    al = (agent_login or "").lower()
    fresh = []
    max_id = last_cid
    max_upd = last_upd or ""
    for c in cs:
        if (c.get("user") or {}).get("login", "").lower() == al:
            continue
        cid = c.get("id", 0)
        upd = c.get("updated_at") or c.get("created_at") or ""
        if cid > last_cid or upd > (last_upd or ""):
            fresh.append(c)
        max_id = max(max_id, cid)
        max_upd = max(max_upd, upd)
    return fresh, max_id, max_upd


def stop_run(cfg, key: str) -> bool:
    """Остановить живой прогон (#25).

    Сначала удалённый hermes (он держит flock и именованную сессию — если убить только
    ssh, сирота продолжит работать и следующие wake будут skip'нуться), затем локальный
    ssh поллера (иначе поллер висит на subprocess.run до таймаута)."""
    b = cfg.get("bridge") or {}
    host = str(b.get("host", "192.168.110.4"))
    user = str(b.get("user", "user"))
    o, r = key.split("/", 1)
    r, num = r.rsplit("#", 1)              # key = owner/repo#num — '#' в workdir нет
    safe = f"{o}-{r}-{num}"
    ok = False
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                           f"{user}@{host}", f"pkill -TERM -f {shlex.quote('agent-work/' + safe)}"],
                          capture_output=True, text=True, timeout=25)
        ok = p.returncode == 0
        if not ok:
            log.warning("stop remote %s: pkill rc=%s (%s)", key, p.returncode, (p.stderr or "").strip()[:120])
    except Exception as e:
        log.warning("stop remote run %s: %s", key, e)
    try:
        subprocess.run(["pkill", "-TERM", "-f", f"ssh -o BatchMode=yes.*{re.escape(host)}"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    return ok


# --------- этапы по времени (NEXT:<минуты>) ---------
NEXT_RE = re.compile(r"NEXT:\s*(\d+)\s*$")


def _parse_next(out):
    """Финальная строка NEXT:<минуты> -> минуты до следующего этапа; иначе None.
    shortcut: clamp 1..43200 (30 суток) — поднимать при появлении задач дольше."""
    if not out:
        return None
    m = NEXT_RE.search(out.strip())
    if not m:
        return None
    return max(1, min(43200, int(m.group(1))))


def _iso_now(minutes=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def stage_waiting(st, now_iso: str) -> bool:
    """Этап ещё не наступил (next_wake в будущем): не будить и очередь не держать.
    shortcut: lexicographic ISO-сравнение — формат один (UTC isoformat) у всех."""
    nw = st.get("next_wake") or ""
    return bool(nw) and nw > now_iso


def stage_due(st, now_iso: str) -> bool:
    """Срок этапа наступил: будить вторым после текущей задачи."""
    nw = st.get("next_wake") or ""
    return bool(nw) and nw <= now_iso
