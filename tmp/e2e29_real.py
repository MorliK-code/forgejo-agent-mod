"""E2E #29 с НАСТОЯЩИМ wake (живая система: Hermes на 110.4 реально просыпается).

Песочница hermes/agent-e2e29r (неполлируемый репо — живой поллер её не видит).
Сценарий заказчика: очередь p5(done) + p4 -> в том же цикле реально будится p4
(агент сам комментирует и ставит себе done), p5(done) остаётся нетронутой.
Затем второй цикл: очередь пуста (done/parked никого не держат и не будятся).

Запуск на CT106 (из /opt/agent-loop/repo, после git pull и рестарта agent-loop):
nohup /opt/agent-loop/venv/bin/python tmp/e2e29_real.py > /tmp/e2e29r.log 2>&1 &
Финальная строка: E2E29 RESULT PASS|FAIL."""
import logging
import os
import shutil
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
os.environ.setdefault("AGENT_RUN_FILE", "/tmp/e2e29r-run.json")  # live run.json не трогаем

sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import bridge

STATE_DIR = "/tmp/e2e29r"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["forgejo"]["paused"] = False
cfg["__path__"] = f"{STATE_DIR}/config.yaml"
cfg["bridge"] = {**(cfg.get("bridge") or {}), "main_chat_idle_gate_s": 0}
O, R = "hermes", "agent-e2e29r"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]

try:
    fj._req("POST", "/user/repos", json={"name": R, "private": False})
except Exception as e:
    print("repo exists?", R, str(e)[:80])

calls = []
_real = bridge.work_task


def real_work_rec(cfg, fj, owner, repo, number, body, new_comments, first):
    calls.append(f"{owner}/{repo}#{number}")
    return _real(cfg, fj, owner, repo, number, body, new_comments, first)


bridge.work_task = real_work_rec

_orig_issues = fj.issues
OWN = set()
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state)
    if (owner, repo) == (O, R) and i["number"] in OWN]


def mk(title, prio, extra=(), body=None):
    n = fj.create_issue(O, R, title, body or "Одноразовая проверка #29. Игнорировать.")["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent"] + list(extra) + [prio])
    return n


D5 = mk("e29r A done p5", "5", extra=["done"])
W4 = mk("e29r B wake p4", "4", body=(
    "Служебная проверка очереди (задача #29 на morlik/Forgejo-agents-tasks).\n"
    "Сделай ровно два действия и закончи прогон словом DONE:\n"
    "1) комментарий к этой задаче: текст `e29-real OK`;\n"
    "2) метки этой задачи через set_labels: agent, 4, done.\n"
    "Больше ничего не делай."))

state = {"done": []}

print("=== poll 1: очередь p5(done)+p4 — ждём реальный wake p4 ===")
before = len(calls)
from agent import poller
poller.poll_once_bridge(cfg, fj, state)
wake1 = calls[before:]
print("WAKE1", wake1)

def labels(n):
    return sorted(fj.issue_labels(O, R, n))

def agent_comments(n):
    return [c for c in (fj.issue_comments(O, R, n) or [])
            if (c.get("user") or {}).get("login", "").lower() == "hermes"]

ok = (wake1 == [f"{O}/{R}#{W4}"]
      and "done" in labels(W4) and agent_comments(W4)
      and labels(D5) == sorted(["agent", "5", "done"])
      and f"{O}/{R}#{D5}" not in state.get("bridge", {})
      and not agent_comments(D5))
print("CHECK labels W4:", labels(W4), "D5:", labels(D5))

print("=== poll 2: очередь после done обеих — никого не держит/не будит ===")
before = len(calls)
poller.poll_once_bridge(cfg, fj, state)
wake2 = calls[before:]
print("WAKE2", wake2)
ok = ok and wake2 == []

for n in OWN:
    try:
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
    except Exception:
        pass

print("E2E29 RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
