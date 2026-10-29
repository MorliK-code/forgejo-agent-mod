"""e2e #19, живой прогон (без заглушки): очередь приоритетов через настоящий мост Hermes.

Песочничный репо hermes/agent-e2e19 (его живой поллер не поллит — гонки нет).
Создаются две задачи: prio5 и prio1. Ожидаем: цикл1 будит ТОЛЬКО prio5 (настоящий
Hermes-прогон: комментарий агента + метка done), prio1 не тронута. Цикл2 — prio1.
"""
import os
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

os.makedirs("/tmp/e2e19real", exist_ok=True)
_LOG = open("/tmp/e2e19real/out.log", "w", buffering=1)
os.dup2(_LOG.fileno(), 1)                        # ssh-канал освобождается сразу:
os.dup2(_LOG.fileno(), 2)                        # прогон живёт в CT, лог — в файле

sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import poller

STATE_DIR = "/tmp/e2e19real"
os.makedirs(STATE_DIR, exist_ok=True)

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["__path__"] = f"{STATE_DIR}/config.yaml"
O, R = "hermes", "agent-e2e19"
fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]

INSTR = ("Проверка очереди приоритетов (одноразовая). Сделай ровно одно: ответь "
         "комментарием 'wake ok' и поставь метку done. Файлов, репозиториев и "
         "коммитов не создавай.")

created = {}
OWN = set()                                   # будим только свои задачи песочницы
for title, prio in (("e2e19 real: задача prio5", "5"), ("e2e19 real: задача prio1", "1")):
    n = fj.create_issue(O, R, title, INSTR)["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent", prio])
    created[prio] = n
P5, P1 = created["5"], created["1"]
K5, K1 = f"{O}/{R}#{P5}", f"{O}/{R}#{P1}"

_orig_issues = fj.issues
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state) if i["number"] in OWN]

state = {"done": []}
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail, flush=True)


def labels(n):
    return sorted(fj.issue_labels(O, R, n))


def agent_comments(n):
    # логин в API — 'Hermes' (регистр не значим, как и в new_user_comments)
    return [c for c in fj.issue_comments(O, R, n)
            if (c.get("user") or {}).get("login", "").lower() == "hermes"]


print(f"P5=#{P5} P1=#{P1}", flush=True)
poller.poll_once_bridge(cfg, fj, state)
a5 = agent_comments(P5)
check("цикл1: prio5 получила настоящий Hermes-wake (комментарий агента)", len(a5) >= 1,
      str(a5[-1:])[:160])
check("цикл1: prio5 in-progress/done", "in-progress" in labels(P5) or "done" in labels(P5),
      str(labels(P5)))
check("цикл1: prio1 НЕ разбужена (гейт приоритета)",
      not agent_comments(P1) and "in-progress" not in labels(P1), str(labels(P1)))

poller.poll_once_bridge(cfg, fj, state)
check("цикл2: после prio5 просыпается prio1", len(agent_comments(P1)) >= 1,
      str(agent_comments(P1)[-1:])[:160])
check("цикл2: метки prio1 = приоритет+agent+in-progress",
      all(x in labels(P1) for x in ("agent", "1", "in-progress")), str(labels(P1)))

for n in (P5, P1):                            # песочницу не мусорим: след. прогон не должен
    try:                                      # наткнуться на прошлые открытые задачи
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
    except Exception:
        pass

bad = [x for x in results if not x[1]]
print("TOTAL", len(results), "FAILS", len(bad), flush=True)
sys.exit(1 if bad else 0)
