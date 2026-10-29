"""E2E #28 с НАСТОЯЩИМ wake: наблюдатель agent-admin убивает зависший прогон.

Песочница hermes/agent-e2e28 (неполлируемый репо; живой agent-loop в это время
остановлен скриптом reload28 — очередей не трогает). Тестовые пороги в оверлее
(reload28): watchdog_idle_s=90, main_chat_idle_gate_s=0.

Сценарий:
  P1 wake#1: агент создаёт маркер и выполняет `sleep 900` (foreground) ->
     сессия молчит > 90с -> наблюдатель убивает прогон (лог 'watchdog: ... тишина');
     полкер возвращает ran=False БЕЗ fails++ (как чат-гейт).
  P1 wake#2: маркер есть -> комментарий `e28-real recovered OK` + метка done ->
     ran=True (доказательство возврата в очередь и нормальной допработки).
  P2: 3x `sleep 15` с активными tool-вызовами между -> тишина < 90с -> НЕ убит,
     ran=True (или честное падение fails>=1 — но не молчание).
Финальная строка: E2E28 RESULT PASS|FAIL."""
import logging
import os
import shutil
import subprocess
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import poller

STATE_DIR = "/tmp/e2e28"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")

cfg = load_live("/opt/agent-loop/config.yaml")
br = cfg.get("bridge") or {}
if int(br.get("watchdog_idle_s", -1)) != 90 or int(br.get("main_chat_idle_gate_s", -1)) != 0:
    print("E2E28 RESULT FAIL (оверлей харнеса не применён: watchdog/gate не те)")
    sys.exit(1)
cfg["forgejo"]["paused"] = False
cfg["__path__"] = f"{STATE_DIR}/config.yaml"
O, R = "hermes", "agent-e2e28"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]

try:
    fj._req("POST", "/user/repos", json={"name": R, "private": False})
except Exception as e:
    print("repo exists?", R, str(e)[:80])

for i in (fj.issues(O, R, "open") or []):      # следы прошлого прогона харнеса не должны мешать
    try:
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{i['number']}", json={"state": "closed"})
    except Exception:
        pass

OWN = set()


def mk(title, body):
    n = fj.create_issue(O, R, title, body)["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent"])
    return n


P1 = mk("e28 hang->recovery", (
    "Служебная проверка watchdog (задача #28 на morlik/Forgejo-agents-tasks).\n"
    "Действуй ровно так и закончи прогон словом DONE:\n"
    "1) Если в рабочей директории НЕТ файла .e28-marker: создай его (terminal: "
    "`touch .e28-marker`) и затем выполни ПОСЛЕДОВАТЕЛЬНО три foreground-команды "
    "terminal: `sleep 300` (каждую с timeout 320, НЕ background; не заменяй другими "
    "командами). Прогон будет прерван наблюдателем — это ожидаемая часть проверки.\n"
    "2) Если .e28-marker ЕСТЬ: комментарий к этой задаче `e28-real recovered OK`; "
    "метки через set_labels: agent, done. Больше ничего не делай."))
P2 = mk("e28 active-not-killed", (
    "Служебная проверка watchdog (задача #28 на morlik/Forgejo-agents-tasks).\n"
    "Действуй ровно так и закончи прогон словом DONE:\n"
    "1) Выполни ПОСЛЕДОВАТЕЛЬНО три foreground-команды terminal: `sleep 15` "
    "(каждую с timeout 60, НЕ background).\n"
    "2) Комментарий к этой задаче `e28-active OK`; метки через set_labels: agent, done."))

K1, K2 = f"{O}/{R}#{P1}", f"{O}/{R}#{P2}"
state = {"done": []}


def bstate(key):
    return state.get("bridge", {}).get(key) or {}


def watchdog_lines():
    p = subprocess.run(["journalctl", "-u", "agent-admin", "--no-pager", "-o", "short"],
                       capture_output=True, text=True, timeout=30)
    return [l for l in (p.stdout or "").splitlines() if "watchdog:" in l]


def agent_comments(n):
    return [c for c in (fj.issue_comments(O, R, n) or [])
            if (c.get("user") or {}).get("login", "").lower() == "hermes"]


def labels(n):
    return sorted(fj.issue_labels(O, R, n))


print("=== фаза A: P1 зависнет (sleep 300) -> watchdog убьёт, ran=False без fails++ ===")
st1, wd1 = {}, []
fails_prev = 0
for _ in range(12):
    fails_prev = st1.get("fails", 0)      # crash-wake (не зависание) fails++ легален
    poller.poll_once_bridge(cfg, fj, state)
    st1 = bstate(K1)
    wd1 = [l for l in watchdog_lines() if K1 in l]
    if wd1 or st1.get("ran") is True:
        break  # watchdog kill (нужно) либо P1 почему-то завершил сам (FAIL)
#ran=False + след watchdog + wakes без повышения fails (kill не «падение» задачи)
c1 = st1.get("ran") is False and bool(wd1) and st1.get("fails", 0) == fails_prev
print("st1", st1, "watchdog_log", wd1)

print("=== фаза B: P1 вернулся в очередь (ran=False) -> доработка до done ===")
st2 = st1
for _ in range(10):
    poller.poll_once_bridge(cfg, fj, state)
    st2 = bstate(K1)
    if st2.get("ran") is True or st2.get("fails", 0) >= 1:
        break
c2 = (st2.get("ran") is True
      and "done" in labels(P1)
      and any("e28-real recovered OK" in (c.get("body") or "") for c in agent_comments(P1)))
print("st2", st2, "labels P1", labels(P1))

print("=== фаза C: P2 активный прогон (3x sleep 15) -> НЕ убит ===")
st3, wd2 = bstate(K2), []
for _ in range(10):
    if not (st3.get("ran") is True or st3.get("fails", 0) >= 1):
        poller.poll_once_bridge(cfg, fj, state)
    st3 = bstate(K2)
    wd_all2 = watchdog_lines()
    wd2 = [l for l in wd_all2 if K2 in l]
    if wd2 or st3.get("ran") is True or st3.get("fails", 0) >= 1:
        break
wd_all = watchdog_lines()
wd_sandbox = [l for l in wd_all if K1 in l or K2 in l]
# «не убит watchdog'ом» = нет строки watchdog по K2; прогон либо завершил (ran=True),
# либо упал честно (fails>=1) — молчаливый ran=False без фейла считался бы убитым
c3 = (not wd2) and (st3.get("ran") is True or st3.get("fails", 0) >= 1)
print("st3", st3, "watchdog_total(all)", wd_all)

ok = c1 and c2 and c3 and len(wd_sandbox) == 1

for n in OWN:
    try:
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
    except Exception:
        pass

print("E2E28 RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
