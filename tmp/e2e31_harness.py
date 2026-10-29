"""e2e-свип #31: last_err в bridge-стейте — причина падения доходит до следующего wake.

Часть 1 (mock, без сети):
- живой bridge.work_task (заменены только _run и _hide_agent_sessions): last_err
  инжектится в wake_line; без last_err строки нет;
- живой poller._bridge_wake (заменён только work_task): фейл -> last_err записан
  в стейт и в файл состояния; следующий wake получает его в kwarg; успех -> снят;
  watchdog/BusySkip -> last_err НЕ пишется (это не работа агента).
Часть 2 (живьём, CT106): песочница hermes/agent-e2e31 (её живой луп не поллит),
реальный Forgejo + реальный цикл поллера + реальный ssh на 110.4:
  wake1: bin=/nonexistent -> реальный rc=127 -> last_err в state-файле, тех-шума
         в комментариях задачи нет;
  wake2: промпт содержит 'предыдущий прогон ... упал' + текст rc=127;
  wake3: bin восстановлен -> РЕАЛЬНЫЙ Hermes видит строку в своём промпте и цитирует
         причину в комментарии задачи; после успеха last_err снят из стейта.
Секреты не печатаются.
"""
import json
import logging
import os
import shutil
import sys

os.environ.setdefault("AGENT_RUN_FILE", "/tmp/e2e31mock-run.json")  # не трогать живой run.json
sys.path.insert(0, os.environ.get("REPO", "/opt/agent-loop/repo"))
logging.basicConfig(level=logging.INFO, format="LOG %(message)s")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


from agent import bridge, poller, tasks

INJECT_MARK = "предыдущий прогон этой задачи упал"
real_run = bridge._run
real_hide = bridge._hide_agent_sessions
real_work = bridge.work_task
real_gate = bridge.chat_gate_active
real_watchdog = bridge.watchdog_idle
real_labels_view = tasks.issue_labels_view

# ============ часть 1: mock ============
prompts = []


def fake_run(cfg, prompt, session=None, workdir=None, timeout=None):
    prompts.append(prompt)
    return 0, "OK DONE", ""


bridge._run = fake_run
bridge._hide_agent_sessions = lambda cfg: None

# 1a. инжект в wake_line на живом work_task
real_work({}, None, "x", "y", 1, "", [], first=True)
check("mock: wake без last_err — строки падения нет", INJECT_MARK not in prompts[-1])
real_work({}, None, "x", "y", 2, "", [], first=False, last_err="RuntimeError: boom-255")
check("mock: wake с last_err — причина в промпте",
      INJECT_MARK in prompts[-1] and "boom-255" in prompts[-1]
      and "Не повторяй этот путь" in prompts[-1])

# 1b. стейт-машина на живом poller._bridge_wake
STATE_DIR = "/tmp/e2e31mock"
shutil.rmtree(STATE_DIR, ignore_errors=True)
os.makedirs(STATE_DIR)
cfg = {"forgejo": {"paused": False, "trigger_labels": ["agent"],
                   "checkbox": "Агент", "user": "hermes"},
       "bridge": {"main_chat_idle_gate_s": 0},
       "__path__": f"{STATE_DIR}/config.yaml"}


class FakeFj:
    def set_labels(self, o, r, n, ls):
        pass


calls = []


def stub_work(cfg, fj, owner, repo, number, body, new_comments, first, last_err=""):
    calls.append(last_err)
    if len(calls) == 1:
        raise RuntimeError("hermes rc=255: mock-hang")
    return 0, "DONE"


bridge.work_task = stub_work
bridge.chat_gate_active = lambda cfg: False
bridge.watchdog_idle = lambda cfg, s, since="": None
tasks.issue_labels_view = lambda cfg, repo, num: {"labels": []}

state = {"done": [], "bridge": {}}
bs = state["bridge"]  # как в poll_once_bridge: bs живёт внутри state (_save_state пишется)
st = {"last_cid": 0, "last_upd": "", "ran": False}
c = {"key": "x/y#9", "owner": "x", "repo": "y", "num": 9,
     "issue": {"number": 9, "title": "t", "body": ""}, "labels": ["agent", "in-progress"],
     "st": st, "fresh": [], "max_id": 0, "max_upd": "", "wake": True, "hold": True,
     "incomplete": True, "stage_due": False, "next_wake": "", "prio": 0}

poller._bridge_wake(cfg, FakeFj(), state, bs, c)  # фейл


def disk_st():
    return json.loads(open(f"{STATE_DIR}/poller_state.json").read()).get("bridge", {}).get("x/y#9", {})


check("mock: фейл -> last_err в стейте и на диске",
      "rc=255" in st.get("last_err", "") and "rc=255" in disk_st().get("last_err", ""))
check("mock: фейл -> fails=1, ran=False (репетить будет)",
      st.get("fails") == 1 and st.get("ran") is False)
err1 = st["last_err"]
poller._bridge_wake(cfg, FakeFj(), state, bs, c)  # успех, wake получает last_err
check("mock: wake1 получил '' (до него падений не было), wake2 — причину wake1",
      calls[0] == "" and calls[1] == err1 and "rc=255" in err1)
check("mock: успех -> last_err снят (стейт+диск), fails=0",
      "last_err" not in st and "last_err" not in disk_st() and st.get("fails") == 0)

# watchdog-стоп = не падение агента: last_err не пишется
def stub_fail(cfg, fj, owner, repo, number, body, new_comments, first, last_err=""):
    calls.append(last_err)
    raise RuntimeError("killed by watchdog")


bridge.work_task = stub_fail
bridge.watchdog_idle = lambda cfg, s, since="": 700
poller._bridge_wake(cfg, FakeFj(), state, bs, c)
check("mock: watchdog-стоп -> last_err НЕ записан, fails не растут",
      "last_err" not in st and st.get("fails") == 0)
bridge.watchdog_idle = lambda cfg, s, since="": None


def stub_busy(cfg, fj, owner, repo, number, body, new_comments, first, last_err=""):
    raise bridge.BusySkip("сессия занята")


bridge.work_task = stub_busy
poller._bridge_wake(cfg, FakeFj(), state, bs, c)
check("mock: BusySkip -> last_err НЕ записан", "last_err" not in st)
print("=== mock done ===")

# ============ часть 2: живьём на CT106 (песочница hermes/agent-e2e31) ============
if not os.path.exists("/opt/agent-loop/env"):
    print("SKIP live part (no /opt/agent-loop/env — run on CT106)")
    fails = [n for n, ok in results if not ok]
    print(f"TOTAL {len(results)} | FAILS {len(fails)}")
    sys.exit(1 if fails else 0)

for _l in open("/opt/agent-loop/env"):
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

from agent.config import load_live
from agent.forgejo_client import ForgejoClient

S = "/tmp/e2e31"
shutil.rmtree(S, ignore_errors=True)
os.makedirs(S)

cfg = load_live("/opt/agent-loop/config.yaml")
prod_bin = str((cfg.get("bridge") or {}).get("bin", "/home/user/.local/bin/hermes"))
cfg["forgejo"]["paused"] = False
cfg["bridge"] = {**(cfg.get("bridge") or {}), "main_chat_idle_gate_s": 0}
cfg["__path__"] = f"{S}/config.yaml"  # состояние харнеса — не живой poller_state.json
O, R = "hermes", "agent-e2e31"

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R
cfg["forgejo"]["tasks_repo"] = f"{O}/{R}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R}"]
cfg["forgejo"]["user"] = "e2e-customer-fake"  # комментарии hermes = 'заказчик' (триггер wake)

bridge._run = real_run
bridge._hide_agent_sessions = real_hide
bridge.work_task = real_work
bridge.chat_gate_active = real_gate
bridge.watchdog_idle = real_watchdog
tasks.issue_labels_view = real_labels_view

_orig_run = bridge._run
live_prompts = []


def rec_run(_cfg, prompt, **kw):  # делегат в НАСТОЯЩИЙ _run (реальный ssh на 110.4)
    live_prompts.append(prompt)
    return _orig_run(_cfg, prompt, **kw)


bridge._run = rec_run

TITLE = "[E2E31]"
own_issues = [i for i in fj.issues(O, R, state="open") if (i.get("title") or "").startswith(TITLE)]
if len(own_issues) == 1:
    fj.set_labels(O, R, own_issues[0]["number"], ["agent"])  # ensure_labels создаёт метки репо
    own_issues = fj.issues(O, R, state="open")
    own_issues = [i for i in own_issues if (i.get("title") or "").startswith(TITLE)]
check("live: песочница готова (1 issue с меткой agent)",
      len(own_issues) == 1 and "agent" in [l["name"] for l in (own_issues[0].get("labels") or [])],
      str([i["number"] for i in own_issues]))
if len(own_issues) != 1:
    fails = [n for n, ok in results if not ok]
    print(f"TOTAL {len(results)} | FAILS {len(fails)}")
    sys.exit(1)
N = own_issues[0]["number"]
_orig_issues = fj.issues
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state) if i["number"] == N]

state = {"done": []}
BAD_BIN = "/nonexistent-e2e31-hermes"
cfg["bridge"]["bin"] = BAD_BIN
woken = poller.poll_once_bridge(cfg, fj, state)
sf = f"{S}/poller_state.json"
d1 = json.loads(open(sf).read()).get("bridge", {}).get(f"{O}/{R}#{N}", {})
check("live wake1: bad-bin реальный фейл -> last_err в state-файле (rc=127)",
      woken == 1 and "127" in d1.get("last_err", ""), (d1.get("last_err") or "")[:120])
check("live wake1: тех-шума в комментариях задачи нет", len(fj.issue_comments(O, R, N)) == 0)

woken = poller.poll_once_bridge(cfg, fj, state)
p2 = live_prompts[-1] if live_prompts else ""
check("live wake2: wake-промпт содержит причину прошлого падения",
      woken == 1 and INJECT_MARK in p2 and "127" in p2)

cfg["bridge"]["bin"] = prod_bin  # реальный Hermes на 110.4
woken = poller.poll_once_bridge(cfg, fj, state)
d3 = json.loads(open(sf).read()).get("bridge", {}).get(f"{O}/{R}#{N}", {})
cs = fj.issue_comments(O, R, N)
agent_quotes = any(("127" in (x.get("body") or "")) or (INJECT_MARK in (x.get("body") or ""))
                   for x in cs)
check("live wake3: реальный агент увидел last_err и процитировал причину в комментарии",
      woken == 1 and agent_quotes, str([(x.get("body") or "")[:90] for x in cs]))
check("live wake3: успех -> last_err снят из state-файла (ran=True, fails=0)",
      "last_err" not in d3 and d3.get("ran") is True and d3.get("fails") == 0)

print("=== live done ===")
fails = [n for n, ok in results if not ok]
print(f"TOTAL {len(results)} | FAILS {len(fails)}")
sys.exit(1 if fails else 0)
