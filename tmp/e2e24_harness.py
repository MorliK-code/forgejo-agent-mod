"""e2e-свип гейта приоритетов (#24) против живого Forgejo.

ВНИМАНИЕ (после #29): часть ожиданий устарела — семантика 0aee011/#29 меняет
гейт: parked без комментов НЕ держит очередь, wake между кулдаунами. Актуальный
регресс очереди: tmp/e2e29_harness.py (тот же паттерн, новые правила).

work_task = stub (без LLM-гонок). Задачи — в неполлируемой песочнице
hermes/agent-e2e24: живой поллер её не видит.
Доказываем: припаркованная незавершённая задача (ran=True, без новых
комментариев — как #21 после 3 падений) ДЕРЖИТ очередь: низшие приоритеты
не стартуют и их комментарии не потребляются; равный приоритет будится;
done/questions/pause очередь не держат; без приоритета — только когда нечего
держать. Секреты не печатаются.
"""
import logging
import os
import shutil
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import bridge, poller, tasks

STATE_DIR = "/tmp/e2e24"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["forgejo"]["paused"] = False          # глобальная пауза панели (#25) не должна глушить wake харнеса
cfg["__path__"] = f"{STATE_DIR}/config.yaml"          # изолированный state-файл
O, R = "hermes", "agent-e2e24"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]
cfg["forgejo"]["user"] = "e2e-customer-fake"          # комменты hermes-токена = «клиентские»

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
OWN = set()          # харнес будит только свои задачи: чужие/прошлые в песочнице не мешают
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state)
    if (owner, repo) == (O, R) and i["number"] in OWN]

state = {"done": []}


def poll():
    before = len(calls)
    poller.poll_once_bridge(cfg, fj, state)
    return calls[before:]


def labels(o, r, n):
    return sorted(fj.issue_labels(o, r, n))


def mk(title, prio=None, extra=()):
    n = fj.create_issue(O, R, title, "Одноразовая проверка гейта #24. Игнорировать.")["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent"] + list(extra) + ([prio] if prio else []))
    return n


def comment(o, r, n, text="c"):
    fj.create_comment(o, r, n, text)


def setl(o, r, n, ls):
    fj.set_labels(o, r, n, ls)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


def untouched(n):
    return "in-progress" not in labels(O, R, n) and f"{O}/{R}#{n}" not in state.get("bridge", {})


A = mk("e24 A prio5", "5")
w = poll()
check("цикл1: wake prio5 (обычный первый захват)", w == [f"{O}/{R}#{A}"], str(w))
check("цикл1: A помечена in-progress", "in-progress" in labels(O, R, A), str(labels(O, R, A)))

# === инцидент #24: припаркованная 5 (прогнана, новых комментариев нет) не пускает 4 ===
B = mk("e24 B prio4", "4")
w = poll()
check("гейт: припаркованная prio5 держит очередь — prio4 НЕ стартует", w == [], str(w))
check("гейт: B не тронута (нет in-progress, комментарии не потреблены)", untouched(B))

C = mk("e24 C prio5", "5")
w = poll()
check("гейт: равный приоритет будится сквозь parked-холд (prio5 wake)", w == [f"{O}/{R}#{C}"], str(w))
check("гейт: prio4 по-прежнему заблокирована", untouched(B))

setl(O, R, A, ["agent", "5", "in-progress", "pause"])     # pause не держит
setl(O, R, C, ["agent", "5", "done"])                     # done не держит
w = poll()
check("гейт снят: parked 5 -> pause + 5 done -> prio4 стартует", w == [f"{O}/{R}#{B}"], str(w))

# parked prio4 держит prio3
D = mk("e24 D prio3", "3")
w = poll()
check("гейт: припаркованная prio4 держит prio3", w == [] and untouched(D), str(w))

setl(O, R, B, ["agent", "4", "in-progress", "questions"]) # questions не держит
w = poll()
check("гейт снят: questions у prio4 -> prio3 стартует", w == [f"{O}/{R}#{D}"], str(w))

# parked prio3 держит безприоритетную
E = mk("e24 E no-prio")
w = poll()
check("гейт: parked prio3 держит безприоритетную", w == [] and untouched(E), str(w))

setl(O, R, D, ["agent", "3", "done"])
w = poll()
check("гейт снят: prio3 done -> безприоритетная стартует", w == [f"{O}/{R}#{E}"], str(w))

# parked без приоритета ничего не держит
F = mk("e24 F no-prio")
w = poll()
check("parked без приоритета не держит очередь", w == [f"{O}/{R}#{F}"], str(w))

# возобновление: комментарий к parked 5 -> она снова кандидат и держит низших
setl(O, R, C, ["agent", "5", "in-progress"])
comment(O, R, C, "вернись")
G = mk("e24 G prio4", "4")
w = poll()
check("возобновлённая parked 5 будится и держит prio4",
      w == [f"{O}/{R}#{C}"] and untouched(G), str(w))

setl(O, R, C, ["agent", "5", "done"])
setl(O, R, A, ["agent", "5", "in-progress", "pause"])
w = poll()
check("очистка холдов -> prio4 догнала", w == [f"{O}/{R}#{G}"], str(w))

# closed не держит и не будится
setl(O, R, A, [])
fj._req("PATCH", f"/repos/{O}/{R}/issues/{G}", json={"state": "closed"})
H = mk("e24 H prio2", "2")
w = poll()
check("closed parked-холда снят; wake prio2 (парковка F/E без приоритета не держит)",
      w == [f"{O}/{R}#{H}"], str(w))

for n in OWN:
    try:
        fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
    except Exception:
        pass
check("после закрытия всех — очередь пуста", poll() == [])

bad = [x for x in results if not x[1]]
print("TOTAL", len(results), "FAILS", len(bad))
sys.exit(1 if bad else 0)
