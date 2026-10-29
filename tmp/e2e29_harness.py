"""Регресс #29 против живого Forgejo (wake — заглушка, LLM не тратится).

Песочница hermes/agent-e2e29 — неполлируемый репо: живой поллер её не видит.
Доказываем правила заказчика на реальных issues/метках/комментариях:
done/questions/pause отпускают очередь немедленно (p4/p3 стартуют в том же
цикле, done-задача не тронута); parked без меток — механика кулдауна (#24).
Запуск на CT106: /opt/agent-loop/venv/bin/python tmp/e2e29_harness.py
(из /opt/agent-loop/repo, после git pull)."""
import logging
import os
import shutil
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
os.environ.setdefault("AGENT_RUN_FILE", "/tmp/e2e29-run.json")  # live run.json не трогаем

sys.path.insert(0, "/opt/agent-loop/repo")
from datetime import datetime, timedelta, timezone

from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import bridge, poller

STATE_DIR = "/tmp/e2e29h"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")


def iso(minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


cfg = load_live("/opt/agent-loop/config.yaml")
cfg["forgejo"]["paused"] = False
cfg["__path__"] = f"{STATE_DIR}/config.yaml"
cfg["bridge"] = {**(cfg.get("bridge") or {}), "main_chat_idle_gate_s": 0,
                 "parked_retry_s": 1800}
O, R = "hermes", "agent-e2e29"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]
cfg["forgejo"]["user"] = "e2e-customer-fake"   # комменты hermes-токена = «клиентские»

try:
    fj._req("POST", "/user/repos", json={"name": R, "private": False})
except Exception as e:
    print("repo exists?", R, str(e)[:80])

calls = []


def fake_work(cfg, fj, owner, repo, number, body, new_comments, first):
    calls.append(f"{owner}/{repo}#{number}")
    return 0, "DONE"


bridge.work_task = fake_work

_orig_issues = fj.issues
OWN = set()          # харнес будит только свои задачи песочницы
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state)
    if (owner, repo) == (O, R) and i["number"] in OWN]

state: dict = {"done": []}


def poll():
    before = len(calls)
    poller.poll_once_bridge(cfg, fj, state)
    return calls[before:]


def key(n):
    return f"{O}/{R}#{n}"


def labels(n):
    return sorted(fj.issue_labels(O, R, n))


def mk(title, prio=None, extra=()):
    n = fj.create_issue(O, R, title, "Одноразовая проверка очереди #29. Игнорировать.")["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent"] + list(extra) + ([prio] if prio else []))
    return n


def comment(n, text="c"):
    fj.create_comment(O, R, n, text)


def setl(n, ls):
    fj.set_labels(O, R, n, ls)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


def untouched(n):
    return "in-progress" not in labels(n) and key(n) not in state.get("bridge", {})


# === тест-кейс 1 заказчика: p5(done) + p4 -> p4 стартует в том же цикле ===
A5 = mk("e29 A done p5", "5", extra=["done"])
A4 = mk("e29 A next p4", "4")
w = poll()
check("кейс1: p5(done) не держит — p4 стартует в том же цикле", w == [key(A4)], str(w))
check("кейс1: p5(done) не тронута (done на месте, state пуст)",
      "done" in labels(A5) and untouched(A5), str(labels(A5)))

# === тест-кейс 2 заказчика: p5(questions) + p3 -> p3 стартует ===
Q5 = mk("e29 Q questions p5", "5", extra=["questions", "in-progress"])
Q3 = mk("e29 Q next p3", "3")
w = poll()
check("кейс2: p5(questions) не держит — p3 стартует", w == [key(Q3)], str(w))
check("кейс2: p5(questions) не тронута поллером", key(Q5) not in state.get("bridge", {}))

# === pause: вне очереди вообще, даже со свежим комментарием ===
P5 = mk("e29 P pause p5", "5", extra=["pause"])
comment(P5, "коммент во время паузы")
P4 = mk("e29 P next p4", "4")
w = poll()
check("pause p5 со свежим комментом: не будится, p4 идёт", w == [key(P4)] and untouched(P5), str(w))

# === parked (ran=True, без комментов) без меток состояния: очередь НЕ блокирует (#29) ===
D5 = mk("e29 D parked p5", "5")
w = poll()
check("обычный первый захват p5", w == [key(D5)] and "in-progress" in labels(D5), str(w))
E4 = mk("e29 D next p4", "4")
w = poll()
check("parked p5 (кулдаун свежий) не держит: p4 стартует сразу", w == [key(E4)], str(w))

# === механика кулдауна (#24): просроченный parked сам будится и держит p4 ===
state.setdefault("bridge", {})[key(D5)]["last_wake"] = iso(-120)   # «спал» 2 часа
e4lw = state["bridge"][key(E4)]["last_wake"]
w = poll()
check("parked p5 с истёкшим кулдауном: будится сам, p4 не тронута",
      w == [key(D5)] and state["bridge"][key(E4)]["last_wake"] == e4lw, str(w))

# === parked отпускает очередь по done-метке немедленно (свежий p4 стартует) ===
setl(D5, ["agent", "5", "done"])
F4 = mk("e29 D fresh p4", "4")
w = poll()
check("parked p5 -> done: очередь отпущена немедленно, свежий p4 стартует",
      w == [key(F4)], str(w))

# === возобновление done по комменту заказчика (#7): будится сквозь холды ===
comment(A5, "вернись, есть уточнение")
w = poll()
check("свежий коммент к done p5: возобновляется (wake), метки: done снят",
      w == [key(A5)] and "in-progress" in labels(A5) and "done" not in labels(A5), str(w))
G3 = mk("e29 G p3", "3")
w = poll()
check("коммент потреблён: done-задача не пересыпается, p3 стартует", w == [key(G3)], str(w))

# === закрытие всех probe-задач: очередь пуста ===
for n in OWN:
    try:
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
    except Exception:
        pass
check("после закрытия всех probe — очередь пуста", poll() == [])

bad = [x for x in results if not x[1]]
print("TOTAL", len(results), "FAILS", len(bad))
sys.exit(1 if bad else 0)
