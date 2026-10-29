"""e2e-свип #30: дедлайн как вторичная сортировка очереди (внутри приоритета).

Часть 1 (mock): фальшивый список кандидатов -> poll_once_bridge, wake-порядок:
prio desc, due asc (просроченные первыми), без due — после всех с due,
внутри группы FIFO created asc, num asc; tz offset не должен ломать порядок.
Часть 2 (живьём): песочница hermes/agent-e2e30 (не поллируется живым лупом),
work_task = stub; реальные issue с due_date через API; порядок старта:
ближайший дедлайн раньше, без дедлайна — последним среди равного prio;
prio4 с просроченным due не обгоняет prio5. Секреты не печатаются.
"""
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.environ.get("REPO", "/opt/agent-loop/repo"))
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


# ============ часть 1: mock-список, без Forgejo ============
from agent import bridge, poller

calls = []


def fake_work(cfg, fj, owner, repo, number, body, new_comments, first):
    calls.append(number)
    return 0, "DONE"


bridge.work_task = fake_work


class FakeFj:
    def set_labels(self, o, r, n, ls):
        pass


def row(num, prio, due=None, created="2026-10-01T00:00:00Z"):
    st = {"last_cid": 0, "last_upd": "", "ran": False}
    return {"key": f"x/y#{num}", "owner": "x", "repo": "y", "num": num,
            "issue": {"number": num, "title": f"t{num}", "body": "",
                      "created_at": created, "due_date": due},
            "labels": ["agent"], "st": st, "fresh": [],
            "max_id": 0, "max_upd": "", "wake": True, "hold": True,
            "incomplete": True, "stage_due": False, "next_wake": "",
            "prio": prio}


STATE_DIR = "/tmp/e2e30mock"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR)
cfg = {"forgejo": {"paused": False, "trigger_labels": ["agent"],
                   "checkbox": "Агент", "user": "hermes"},
       "bridge": {"main_chat_idle_gate_s": 0},   # чат-гейт (#chat) в харнесе не нужен
       "__path__": f"{STATE_DIR}/config.yaml"}

_orig_candidates = poller._bridge_candidates


def run_mock(rows):
    """Циклы поллера на фальшивом списке: wake только у непрогнанных;
    после wake задача «done» (#29: ran=True не держит очередь) — как в живом цикле."""
    calls.clear()
    poller._bridge_candidates = lambda *a: [dict(r, wake=not r["st"]["ran"],
                                                 hold=not r["st"]["ran"]) for r in rows]
    state = {"done": []}
    while poller.poll_once_bridge(cfg, FakeFj(), state):
        pass
    return list(calls)


# приоритет внутри: просроченный due -> ближайший due -> дальний due -> без due (created asc)
rows = [row(10, 5, "2026-10-10T12:00:00Z"),                       # просроченный
        row(11, 5, None, "2026-10-02T00:00:00Z"),                 # без due, создан раньше
        row(12, 5, "2026-12-01T12:00:00Z"),                       # дальний due
        row(13, 5, "2026-11-01T12:00:00Z"),                       # ближайший due
        row(14, 5, None, "2026-10-03T00:00:00Z")]                 # без due, позже
check("mock: порядок wake = due asc (overdue first), nulls last, FIFO внутри",
      run_mock(rows) == [10, 13, 12, 11, 14], str(calls))

# tz: '05:00+03' абсолютнее раньше, чем '07:00Z' того же дня — строковый sort дал бы наоборот
rows = [row(20, 5, "2026-11-01T07:00:00Z"),
        row(21, 5, "2026-11-01T05:00:00+03:00")]
check("mock: due сравнивается по абсолютному времени (tz offset)",
      run_mock(rows) == [21, 20], str(calls))

# приоритет главнее дедлайна: prio5 без due стартует раньше prio4 с просроченным due
rows = [row(30, 4, "2020-01-01T12:00:00Z"),
        row(31, 5, None)]
check("mock: prio главнее due (5 без due раньше 4 просроченной)",
      run_mock(rows) == [31, 30], str(calls))

# без приоритета — только когда нечего держать по приоритету (старое поведение intact)
rows = [row(40, 0, None), row(41, 3, None)]
check("mock: приоритетная задача блокирует бесприоритетную",
      run_mock(rows) == [41, 40], str(calls))

# битый due_date не роняет поллер, трактуется как «без дедлайна»
rows = [row(50, 5, "не-дата"), row(51, 5, "2026-11-01T12:00:00Z")]
check("mock: битый due_date -> nulls last, без падения",
      run_mock(rows) == [51, 50], str(calls))

print("=== mock done ===")

# ============ часть 2: живьём, песочница hermes/agent-e2e30 ============
if not os.path.exists("/opt/agent-loop/env"):
    print("SKIP live part (no /opt/agent-loop/env — run on CT106)")
    fails = [n for n, ok in results if not ok]
    print(f"TOTAL {len(results)} | FAILS {len(fails)}")
    sys.exit(1 if fails else 0)

poller._bridge_candidates = _orig_candidates
for _l in open("/opt/agent-loop/env"):
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

from agent.config import load_live
from agent.forgejo_client import ForgejoClient

S = "/tmp/e2e30"
shutil.rmtree(S, ignore_errors=True)
os.makedirs(S)

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["forgejo"]["paused"] = False
cfg["bridge"] = {"main_chat_idle_gate_s": 0}   # харнес не должен ждать тишины главного чата
cfg["__path__"] = f"{S}/config.yaml"
O, R = "hermes", "agent-e2e30"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]
cfg["forgejo"]["user"] = "e2e-customer-fake"

try:
    fj._req("POST", "/user/repos", json={"name": R, "private": False})
except Exception as e:
    print("repo exists?", R, str(e)[:80])

calls.clear()
state = {"done": []}
OWN = set()


def fake_work_live(cfg, fj, owner, repo, number, body, new_comments, first):
    calls.append(f"{owner}/{repo}#{number}")
    return 0, "DONE"


bridge.work_task = fake_work_live
_orig = fj.issues
fj.issues = lambda owner, repo, state="open": [i for i in _orig(owner, repo, state)
                                               if (owner, repo) == (O, R) and i["number"] in OWN]


def poll():
    before = len(calls)
    poller.poll_once_bridge(cfg, fj, state)
    return calls[before:]


def mk(title, prio, due=None):
    n = fj.create_issue(O, R, title, "Одноразовая проверка #30. Игнорировать.",
                        due_date=due)["number"]
    OWN.add(n)
    fj.set_labels(O, R, n, ["agent", prio])
    return n


def done(n, prio):
    fj.set_labels(O, R, n, ["agent", prio, "done"])


now = datetime.now(timezone.utc)
D = lambda days: (now + timedelta(days=days)).strftime("%Y-%m-%dT12:00:00Z")

E1 = mk("e30 overdue", "5", D(-3))       # просроченный
E2 = mk("e30 no-due", "5", None)         # без дедлайна
E3 = mk("e30 due+30", "5", D(30))        # дальний
E4 = mk("e30 due+1", "5", D(1))          # ближайший
E5 = mk("e30 prio4 overdue", "4", D(-3)) # prio ниже, due просрочен

order = []
for _ in range(5):
    w = poll()
    if not w:
        break
    num = int(w[0].split("#")[1])
    order.append(num)
    done(num, "5" if num != E5 else "4")
check("живьём: API вернул due_date в списке issues",
      all(i.get("due_date") is not None for i in _orig(O, R)
          if i["number"] in (E1, E3, E4)), "")
check("живьём: старт по ближайшему дедлайну, без due последним в prio, prio4 после prio5",
      order == [E1, E4, E3, E2, E5], str(order))
check("живьём: шестой цикл пуст (всё done)", poll() == [], "")

fails = [n for n, ok in results if not ok]
print(f"TOTAL {len(results)} | FAILS {len(fails)}")
for n in fails:
    print("FAIL:", n)
sys.exit(1 if fails else 0)
