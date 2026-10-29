"""Poller: ищет новые задачи (label из trigger_labels или снятый чекбокс) и запускает агента.

Конфиг перечитывается каждый цикл (load_live) — панель управления пишет
config.overlay.yaml и агент подхватывает изменения без рестарта.
Состояние (последние обработанные issue) — в JSON-файле рядом с конфигом.
Одна задача за раз (очередь). Ошибки — в лог, issue помечается комментарием.
"""
from __future__ import annotations

import json
import logging
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agent as agent_mod
from . import bridge as bridge_mod
from . import tasks as tasks_mod
from .config import load, load_live
from .forgejo_client import ForgejoClient, ForgejoError
from .honcho_client import Honcho

log = logging.getLogger("poller")

STOP = False


def _stop(signum, frame):
    global STOP
    STOP = True
    log.info("stop signal %s", signum)


def setup_logging(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(path, encoding="utf-8")],
    )


def _state_file(cfg_path: str) -> Path:
    return Path(cfg_path).parent / "poller_state.json"


def _load_state(cfg_path: str) -> dict:
    p = _state_file(cfg_path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"done": []}


def _save_state(cfg_path: str, state: dict):
    _state_file(cfg_path).write_text(json.dumps(state, indent=1))


def _has_agent_trigger(issue: dict, labels: list[str], trigger_labels: list[str], checkbox: str) -> bool:
    if any(l in trigger_labels for l in labels):
        return True
    body = issue.get("body", "") or ""
    return re.search(rf"-\s*\[x\]\s*\**{re.escape(checkbox)}", body, re.I) is not None


def poll_once(cfg, fj, honcho, state: dict) -> int:
    """Один цикл поллинга. Возвращает число запущенных задач.

    Опрашиваются все репо из tasks.targets(): рабочий репо + служебный туду-репо
    (forgejo.tasks_repo). Ключ состояния — 'owner/repo#number' (номера в разных
    репо не пересекаются)."""
    trigger = cfg["forgejo"].get("trigger_labels", ["agent"])
    checkbox = cfg["forgejo"].get("checkbox", "Агент")
    done = set(state["done"])
    launched = 0

    for owner, repo in tasks_mod.targets(cfg):
        if not repo:
            continue
        for issue in fj.issues(owner, repo, state="open"):
            num = issue["number"]
            key = f"{owner}/{repo}#{num}"
            if key in done or num in done:
                continue
            labels = issue.get("labels") and [l["name"] for l in issue["labels"]] or []
            if not _has_agent_trigger(issue, labels, trigger, checkbox):
                continue

            log.info("picking up %s: %s", key, issue["title"])
            # помечаем, чтобы не дублировать (даже при сбое)
            done.add(key)
            state["done"] = sorted(done)
            _save_state(cfg["__path__"], state)
            try:
                keep = [l for l in labels if l not in trigger]
                fj.remove_labels(owner, repo, num, trigger)
                # add_labels только ДОБАВЛЯЕТ (замена — set_labels) — возвращаем то, что было
                fj.add_labels(owner, repo, num, keep + ["in-progress"])
                # какая модель реально будет — из models.json оверлея + блока ### Модель
                providers, dflt = agent_mod.effective_models(cfg)
                model = agent_mod.pick_model_from_body(issue.get("body", ""), list(providers.keys()), dflt)
                fj.create_comment(owner, repo, num,
                                  f"**Агент** берёт задачу в работу (model: {model}).")
                agent_mod.run_task(cfg, fj, honcho, issue)
            except Exception as e:
                log.exception("task %s failed: %s", key, e)
                try:
                    fj.create_comment(owner, repo, num, f"**Агент** упал: {type(e).__name__}: {e}")
                    fj.remove_labels(owner, repo, num, ["in-progress"])
                except ForgejoError:
                    pass
            launched += 1
    return launched


def _parked_expired(cfg, st: dict, hold: bool) -> bool:
    """(#24) Припаркованная незавершённая (ran=True, свежих комментариев нет) будится
    по cooldown parked_retry_s (default 1800с), а не навсегда — иначе она держит
    очередь и сама не работает: живой системы нет."""
    if not hold or not st.get("ran"):
        return False
    lw = st.get("last_wake") or ""
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(lw)).total_seconds()
    except ValueError:
        return True  # нет отметки времени — разбудить один раз для установки
    return age >= int((cfg.get("bridge") or {}).get("parked_retry_s", 1800))


def _bridge_candidates(cfg, fj, bs, trigger, checkbox, agent_login) -> list[dict]:
    """Глобальная очередь (#19): все агентские задачи по всем поллируемым репо.

    Флаг 'wake' — будить сейчас: свежий комментарий не-агента (возобновление —
    работает и для done/questions, правило 7), первый захват (ТОЛЬКО незавершённых),
    срок запланированного этапа (NEXT), кулдаун парковки (#24).
    Флаг 'hold' (#29) — очередь держит только незавершённая И готовая к запуску
    (wake=True) задача. done/questions/pause отпускают очередь немедленно: без
    свежих комментариев они не будятся и не держат; при первом захвате done-задача
    не должна воровать цикл и стирать метку заказчика (#29). Ждущая этап/кулдаун —
    не держит. 'pause' в очередь не попадает вообще (комментарии копятся и придат
    после снятия паузы)."""
    out = []
    for owner, repo in tasks_mod.targets(cfg):
        if not repo:
            continue
        for issue in fj.issues(owner, repo, state="open"):
            num = issue["number"]
            key = f"{owner}/{repo}#{num}"
            labels = [l["name"] for l in (issue.get("labels") or [])]
            if not _has_agent_trigger(issue, labels, trigger, checkbox):
                continue
            if "pause" in labels:
                continue
            now_iso = datetime.now(timezone.utc).isoformat()
            st = bs.get(key) or {"last_cid": 0, "last_upd": "", "ran": False}
            try:
                fresh, max_id, max_upd = bridge_mod.new_user_comments(
                    fj, owner, repo, num, st["last_cid"], st.get("last_upd", ""), agent_login)
            except ForgejoError as e:
                log.error("comments fetch %s: %s", key, e)
                continue
            incomplete = "done" not in labels and "questions" not in labels
            # первый захват будит только незавершённые: done/questions без состояния
            # не должны воровать wake-цикл и стирать метку заказчика (#29);
            # возобновление сделанной задачи — только по свежему комменту или этапу
            wake_now = (bool(fresh) or (incomplete and not st["ran"])
                        or bridge_mod.stage_due(st, now_iso)
                        or _parked_expired(cfg, st, incomplete))
            out.append({"key": key, "owner": owner, "repo": repo, "num": num,
                        "issue": issue, "labels": labels, "st": st, "fresh": fresh,
                        "max_id": max_id, "max_upd": max_upd,
                        "incomplete": incomplete,
                        # очередь держит только готовая к запуску незавершённая (#29):
                        # ждущая свой этап/кулдаун — не держит
                        "hold": incomplete and wake_now,
                        "wake": wake_now,
                        "stage_due": bridge_mod.stage_due(st, now_iso),
                        "next_wake": st.get("next_wake") or "",
                        "prio": tasks_mod.priority_of(labels)})
    return out


def _bridge_wake(cfg, fj, state: dict, bs, c: dict) -> None:
    """Один wake-прогон по задаче из очереди."""
    owner, repo, num, key = c["owner"], c["repo"], c["num"], c["key"]
    issue, labels, st = c["issue"], c["labels"], c["st"]
    st["last_cid"] = c["max_id"]  # до прогона: свои комментарии агента не будят повторно
    st["last_upd"] = c["max_upd"]
    st["last_wake"] = datetime.now(timezone.utc).isoformat()  # cooldown припаркованного retries (#24)
    bs[key] = st
    _save_state(cfg["__path__"], state)
    # задача в работе: 'done'/'questions' снимаем, 'in-progress' ставим; приоритеты
    # и прочие метки заказчика копируем как есть (set_labels = PUT-замена всего набора),
    # кроме дедупа приоритетов (#17): у задачи один приоритет — наивысший
    keep = tasks_mod.normalize_priorities(
        [l for l in labels if l not in ("done", "questions", "backlog")] + ["in-progress"])
    if [l for l in keep if l in tasks_mod.PRIORITIES] != [l for l in labels if l in tasks_mod.PRIORITIES]:
        log.info("wake %s: дедуп приоритетов %s -> %s", key,
                 [l for l in labels if l in tasks_mod.PRIORITIES],
                 [l for l in keep if l in tasks_mod.PRIORITIES])
    if keep != labels:
        try:
            fj.set_labels(owner, repo, num, keep)
        except ForgejoError as e:
            log.error("wake labels %s: %s", key, e)
    try:
        code, out = bridge_mod.work_task(cfg, fj, owner, repo, num, issue.get("body") or "",
                                         c["fresh"], first=not st["ran"],
                                         last_err=st.get("last_err", ""))
        st["ran"] = True
        st["fails"] = 0
        st.pop("last_err", None)  # #31: после успеха причину прошлого падения не несём
        nxt = bridge_mod._parse_next(out)  # NEXT:<минуты> — план следующего этапа (#stages)
        if nxt is not None:
            st["next_wake"] = bridge_mod._iso_now(nxt)
            st["stage"] = int(st.get("stage", 0)) + 1
            log.info("wake %s: этап %d запланирован через %d мин", key, st["stage"], nxt)
        elif st.get("next_wake"):
            st.pop("next_wake", None)
            st["stage"] = 0
        _save_state(cfg["__path__"], state)
        # 'in-progress' НЕ снимаем: задача за агентом до done (правило заказчика);
        # 'done' ставит агент, при следующем wake поллер снимет done и in-progress
    except bridge_mod.BusySkip as e:
        log.info("wake %s skipped: %s", key, e)
    except Exception as e:
        # тех-шум не пишем в задачу (политика заказчика): только журнал + счётчик
        log.exception("work run %s failed", key)
        # #25: прогон остановлен паузой (глобальной или меткой 'pause') — задача не
        # «отработала», она должна вернуться в очередь после снятой паузы
        try:
            names = [l.get("name") if isinstance(l, dict) else l
                     for l in (tasks_mod.issue_labels_view(cfg, f"{owner}/{repo}", num)
                               .get("labels") or [])]
        except Exception:
            names = []
        if (cfg["forgejo"].get("paused") or "pause" in names
                # #28: прогон убил watchdog (тишина work-сессии) — это не работа агента:
                # вернуть в очередь без fails++ (ran=False), как при чат-гейте.
                # Отметка наблюдателя — надёжнее метра тишины: убитый TERM'ом hermes
                # успевает дописать сообщение и «молчание» на учёте уже не видна.
                or bridge_mod.watchdog_stop_marked(key)
                or bridge_mod.chat_gate_active(cfg)
                or bridge_mod.watchdog_idle(cfg, bridge_mod.session_name(owner, repo, num),
                                            st.get("last_wake", "")) is not None):
            log.info("work run %s остановлен паузой/чат-гейтом/watchdog — задача ждёт снятия", key)
            st["ran"] = False  # не паркуем и не считаем падением: это не работа агента
            _save_state(cfg["__path__"], state)
            return
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= 3:
            st["ran"] = True  # авто-репетить больше не будем, ждём комментарий/правка
        # #31: короткая причина падения в bridge-стейт — следующий wake подложит её
        # в wake_line (в комментарии задачи не попадает — политика заказчика)
        st["last_err"] = f"{type(e).__name__}: {e}"[:300]
        _save_state(cfg["__path__"], state)


NO_DUE = datetime.max.replace(tzinfo=timezone.utc)


def _due_key(c: dict) -> tuple:
    """Дедлайн due_date для sort-ключа очереди (#30): (0, dt) — есть; (1, NO_DUE) — нет.

    nulls last: без дедлайна — после всех с дедлайном; просроченные первыми
    (due asc). Из-за tz offset строковое сравнение ISO неверно — сравниваем
    абсолютное время; битый due_date = «без дедлайна» (не роняет поллер)."""
    d = c["issue"].get("due_date")
    if d:
        try:
            dt = datetime.fromisoformat(d)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (0, dt)
        except ValueError:
            log.warning("bad due_date %r in %s — считаем без дедлайна", d, c["key"])
    return (1, NO_DUE)


def _pick_top(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Идеальная очередь (#queue, заказчика: одна, без параллельности):
    gate — не будить ниже максимального приоритета среди НЕЗАВЕРШЁННЫХ ГОТОВЫХ
    к запуску (hold); разбуженный этап (stage_due) идёт ПЕРВЫМ по запланированному
    времени — «вторая после текущей» — и gate его не касается.
    waiting — незавершённые, ждущие своего этапа/кулдауна: очередь не держат (#29).
    Порядок: stage_due (next_wake asc), затем prio desc, due asc nulls last (#30:
    ближайший дедлайн раньше, просроченные первыми, без дедлайна — после всех
    с дедлайном), created asc (старее раньше), num.
    Возвращает (кандидаты в порядке, waiting)."""
    hold_p = max((r["prio"] for r in rows if r["hold"]), default=0)
    waiting = [r for r in rows if r["incomplete"] and not r["wake"]]
    cands = [c for c in rows if c["wake"] and (c["stage_due"] or c["prio"] >= hold_p)]
    cands.sort(key=lambda c: (0 if c["stage_due"] else 1,
                              c["next_wake"], -c["prio"], _due_key(c),
                              c["issue"].get("created_at") or "", c["num"]))
    return cands, waiting


def poll_once_bridge(cfg, fj, state: dict) -> int:
    """Bridge-режим: исполнение не у нас, а у Hermes (110.4), по одному wake-прогону.

       Wake: метка agent + новый/отредактированный комментарий не-агента (или первый захват).
    Метка 'pause' — задачу не будим вообще (комментарии копятся и придат при снятии паузы).
    fmt_q: очередь «переформатировать комментарий» (пишет панель, исполняем здесь).
    Состояние: state['bridge'][key] = {last_cid, last_upd, ran, last_err} (#31:
    last_err — причина последнего падения, читается следующим wake, снимается успехом).
    done метку ставит сам Hermes через MCP; 'in-progress' липкий: ставим на wake,
    снимаем только когда агент поставил done (или задачу взяли в работу заново —
    тогда wake снимает 'done'/'questions').
    Очередь приоритетов (#19/#29, заказчика: ОДНА очередь, без параллельности): за цикл
    будим ОДНУ задачу — приоритет 5→1; равный приоритет — ближайший дедлайн
    раньше (#30: просроченные первыми, без дедлайна — после всех с дедлайном),
    внутри группы — старее (created asc).
    Держит очередь только незавершённая ГОТОВАЯ к запуску (wake=True): задачи, ждущие
    своего этапа (NEXT) или кулдауна после фейлов, очередь НЕ держат (#29).
    Разбуженный этап задачи по времени идёт ПЕРВЫМ в цикле — «вторая после текущей»
    (#stages): план этапа пишет агент (NEXT:<минуты>), хранится в bridge-стейте.
    shortcut: последовательный цикл (один прогон за раз) — при параллельных задачах
    перейти на фоновые Popen с проверкой pid.
    """
    if cfg["forgejo"].get("paused"):
        # Пауза (#25) = метки: 'pause' на всех открытых агентских задачах, новые
        # получают её автоматически; wake при включённой паузе не делаем вообще.
        try:
            tasks_mod.set_global_pause(cfg, True, fj)
        except Exception as e:
            log.warning("pause labels: %s", e)
        return 0
    trigger = cfg["forgejo"].get("trigger_labels", ["agent"])
    checkbox = cfg["forgejo"].get("checkbox", "Агент")
    agent_login = str(cfg["forgejo"].get("user", "hermes"))
    bs = state.setdefault("bridge", {})
    # fmt-запросы пишутся в файл из панели — перечитываем каждый цикл (источник истины — файл)
    # ВАЖНО: мержим только fmt_* — runtime-поля bridge-стейта (last_wake/fails/ran) живут в
    # памяти цикла; перечитывание файла каждый цикл затирало last_wake и мультипликало wake.
    file_bs = _load_state(cfg["__path__"]).get("bridge", {})
    for k, fv in file_bs.items():
        mv = bs.get(k)
        if mv is None:
            bs[k] = fv
        else:
            for fk, fv2 in fv.items():
                if fk.startswith("fmt_"):
                    mv[fk] = fv2

    def _p(c):
        return f"p{c['prio']}" if c["prio"] else "без приоритета"

    def _q(c):
        return f"{c['key']}[{_p(c)}]"

    rows = _bridge_candidates(cfg, fj, bs, trigger, checkbox, agent_login)
    cands = [r for r in rows if r["wake"]]
    if not cands:
        return 0
    # чат-гейт: основной чат Hermes = приоритет №1 — пока в нём активность,
    # никого не будим (idle метр — state.db на 110.4, fail-open при сбое)
    gate_s = int((cfg.get("bridge") or {}).get("main_chat_idle_gate_s", 180))
    if gate_s > 0:
        idle = bridge_mod.main_chat_idle(cfg)
        if idle is not None and idle < gate_s:
            log.info("queue: main chat active (idle %ds < gate %ds) — hold", int(idle), gate_s)
            return 0
    # гейт очереди (#29): держит только готовая к запуску незавершённая;
    # ждущие этапа/кулдауна очередь не блокируют; этап-срок — всегда первым
    cands, waiting = _pick_top(rows)
    if not cands:
        if waiting:
            log.info("queue: будить нечего (ждут этап/кулдаун: %s)",
                     ", ".join(_q(r) for r in waiting[:8]))
        return 0
    top = cands[0]

    if top["stage_due"]:
        log.info("queue: wake %s [%s, этап %s по плану] — вторая после текущей",
                 top["key"], _p(top), top["st"].get("stage", "?"))
    else:
        log.info("queue: wake %s [%s] | ждут: %s", top["key"], _p(top),
                 ", ".join(_q(c) for c in cands[1:8]) or "нет")
    _bridge_wake(cfg, fj, state, bs, top)
    return 1


def _forgejo_state(cfg) -> tuple[str, tuple]:
    """Ключи подключения к Forgejo + подписка на изменения конфига."""
    key = (cfg["forgejo"]["url"], cfg["forgejo"]["user"],
           cfg["forgejo"]["token"], cfg["forgejo"]["owner"], cfg["forgejo"]["repo"],
           str(cfg["forgejo"].get("tasks_repo") or ""))
    return key[0], key  # (url, key)


def main(cfg_path: str):
    cfg = load_live(cfg_path)
    log_path = cfg.get_path("agent", "log", default="/var/log/agent-loop/agent.log")
    setup_logging(log_path)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
    honcho = _build_honcho(cfg)

    # метки поллируемых репо: недостающие создать, приоритетные перекрасить
    # под новую семантику (#19: 5 = самый важный)
    need = list(tasks_mod.PRIORITIES) + ["agent", "in-progress", "done", "questions",
                                         "pause", "review", "todo", "backlog"]
    for o, r in tasks_mod.targets(cfg):
        if not r:
            continue
        try:
            fj.ensure_labels(o, r, need, tasks_mod.LABEL_COLORS)
        except ForgejoError as e:
            log.warning("labels %s/%s: %s", o, r, e)

    state = _load_state(cfg["__path__"])
    interval = cfg["forgejo"].get("poll_interval", 15)
    log.info("agent-loop started (interval=%ss, default model=%s)",
             interval, cfg.get_path("models", "default"))
    last_key = _forgejo_state(cfg)[1]
    while not STOP:
        try:
            # живой конфиг: оверлей панели подхватывается без рестарта
            cfg = load_live(cfg_path)
            key = _forgejo_state(cfg)[1]
            if key != last_key:
                log.info("forgejo connection settings changed — rebuilding client")
                fj = ForgejoClient(*key[:3])
                honcho = _build_honcho(cfg)
                last_key = key
            mode = cfg.get_path("agent", "mode", default="bridge")
            if mode == "bridge":
                poll_once_bridge(cfg, fj, state)
            else:
                poll_once(cfg, fj, honcho, state)
        except ForgejoError as e:
            log.error("forgejo error: %s", e)
        except Exception:
            log.exception("poll cycle error")
        interval = 3 if cfg["forgejo"].get("paused") else cfg["forgejo"].get("poll_interval", 15)
        # снапс по секундам, чтобы SIGTERM отработал быстро
        for _ in range(interval * 4):
            if STOP:
                break
            time.sleep(0.25)
    log.info("agent-loop stopped")


def _build_honcho(cfg):
    hcfg = cfg.get("honcho")
    if hcfg and hcfg.get("api_key"):
        return Honcho(hcfg["base_url"], hcfg["api_key"], hcfg["workspace"],
                      hcfg["peer"], hcfg["ai_peer"])
    return None
