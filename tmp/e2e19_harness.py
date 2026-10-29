"""e2e-свип очереди приоритетов (#19) против живого Forgejo.

work_task = stub (без LLM-гонок). Задачи — в неполлируемых песочницах
hermes/agent-e2e19 (репо 1) и hermes/agent-e2e19b (репо 2): живой поллер их не видит.
Доказываем: глобальная очередь по всем репо, 5 > ... > 1 > без приоритета,
жёсткий гейт (пока есть кандидат выше — ниже не стартует), ровно один приоритет
на задачу, перекраска меток 5..1, closed/pause не будятся.
Секреты не печатаются.
"""
import os
import sys

for _l in open("/opt/agent-loop/env"):          # секреты лупа (FORGEJO_TOKEN и т.д.)
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import bridge, poller, tasks

STATE_DIR = "/tmp/e2e19"
os.makedirs(STATE_DIR, exist_ok=True)

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["forgejo"]["paused"] = False          # глобальная пауза панели (#25) не глушит харнес
cfg["__path__"] = f"{STATE_DIR}/config.yaml"          # изолированный state-файл: харнес не трогает живой
O, R1, R2 = "hermes", "agent-e2e19", "agent-e2e19b"
SANDBOX = {(O, R1), (O, R2)}

fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"] = O
cfg["forgejo"]["repo"] = R1
cfg["forgejo"]["tasks_repo"] = f"{O}/{R1}"
cfg["forgejo"]["tasks_repos"] = [f"{O}/{R1}", f"{O}/{R2}"]
cfg["forgejo"]["user"] = "e2e-customer-fake"          # комменты hermes-токена = «клиентские»

for r in (R1, R2):
    try:
        fj._req("POST", "/user/repos", json={"name": r, "private": False})
    except Exception as e:
        print("repo exists?", r, str(e)[:80])

calls = []


def fake_work(cfg, fj, owner, repo, number, body, new_comments, first):
    calls.append(f"{owner}/{repo}#{number}")
    return 0, "DONE"


bridge.work_task = fake_work

_orig_issues = fj.issues
OWN = set()          # харнес будит только свои задачи: чужие/прошлые в песочнице не мешают
fj.issues = lambda owner, repo, state="open": [
    i for i in _orig_issues(owner, repo, state)
    if (owner, repo) in SANDBOX and i["number"] in OWN]

state = {"done": []}


def poll():
    before = len(calls)
    poller.poll_once_bridge(cfg, fj, state)
    return calls[before:]


def labels(o, r, n):
    return sorted(fj.issue_labels(o, r, n))


def mk(repo, title, prio=None, extra=()):
    n = fj.create_issue(O, repo, title, "Одноразовая проверка очереди #19. Игнорировать.")["number"]
    OWN.add(n)
    fj.set_labels(O, repo, n, ["agent"] + list(extra) + ([prio] if prio else []))
    return n


def comment(o, r, n, text="c"):
    fj.create_comment(o, r, n, text)


def setl(o, r, n, ls):
    fj.set_labels(o, r, n, ls)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, detail)


A = mk(R1, "e2e19 A prio5", "5")
B = mk(R1, "e2e19 B prio3", "3")
C = mk(R1, "e2e19 C no-prio")
D = mk(R1, "e2e19 D prio 1+2", extra=("1", "2"))
E = mk(R2, "e2e19 E prio5 in other repo", "5")

try:
    # 1..5. Глобальная очередь (#19) с семантикой #24: незавершённая задача
    # (нет done/questions/pause) держит очередь, даже припаркованная; низшие
    # приоритеты ждут, пока её сделают/снимут вопрос/поставят паузу.
    w = poll()
    check("цикл1: ровно один wake, prio5 (FIFO внутри равного приоритета)",
          w == [f"{O}/{R1}#{A}"], str(w))
    check("цикл1: низшие приоритеты не тронуты (нет in-progress)",
          all("in-progress" not in labels(O, R1, n) for n in (B, C, D))
          and "in-progress" not in labels(O, R2, E),
          str([(n, labels(O, R1, n)) for n in (B, C, D)] + [(f"R2#{E}", labels(O, R2, E))]))
    w = poll()
    check("цикл2: очередь глобальна — wake prio5 из другого репо",
          w == [f"{O}/{R2}#{E}"], str(w))
    w = poll()
    check("цикл3 (#24): припаркованные prio5 A и E держат очередь — 3/2/без-приоритета спят",
          w == [] and all("in-progress" not in labels(O, R1, n) for n in (B, C, D)), str(w))
    setl(O, R1, A, ["agent", "5", "done"])      # симуляция: агент закрыл обе 5-ки
    setl(O, R2, E, ["agent", "5", "done"])
    w = poll()
    check("цикл4: после закрытия prio5 стартует prio3", w == [f"{O}/{R1}#{B}"], str(w))
    w = poll()
    check("цикл5 (#24): припаркованная prio3 B держит prio2",
          w == [] and "in-progress" not in labels(O, R1, D), str(w))
    setl(O, R1, B, ["agent", "3", "done"])
    w = poll()
    check("цикл6: prio2 = максимум из {1,2}", w == [f"{O}/{R1}#{D}"], str(w))
    check("цикл6: дедуп приоритетов — у D остался один приоритет '2'",
          [x for x in labels(O, R1, D) if x in tasks.PRIORITIES] == ["2"],
          str(labels(O, R1, D)))
    setl(O, R1, D, ["agent", "2", "done"])
    w = poll()
    check("цикл7: без приоритета — только когда нечего будить с приоритетом",
          w == [f"{O}/{R1}#{C}"], str(w))
    check("цикл8: очередь пуста — никого не будим", poll() == [])

    # 4. жёсткий гейт: свежая активность prio3 блокирует безприоритетную задачу
    ckey = f"{O}/{R1}#{C}"
    cid_before = state.get("bridge", {}).get(ckey, {}).get("last_cid")
    comment(O, R1, B, "вернись")
    comment(O, R1, C, "я жду")
    w = poll()
    check("гейт: пока есть кандидат prio3, задача без приоритета не стартует",
          w == [f"{O}/{R1}#{B}"], str(w))
    cid_after = state.get("bridge", {}).get(ckey, {}).get("last_cid")
    check("гейт: комментарии безприоритетной задачи не потреблены (ждёт следующего цикла)",
          cid_after == cid_before, f"{cid_before} -> {cid_after}")

    # 5. pause у prio5 не держит очередь (задача припаркована — агенту делать нечего)
    setl(O, R1, B, ["agent", "3", "done"])     # снимаем припаркованную prio3 (она бы держала)
    setl(O, R1, A, ["agent", "5", "in-progress", "pause"])
    comment(O, R1, A, "после паузы")
    comment(O, R1, C, "можно мне?")
    w = poll()
    check("pause prio5: не будится", f"{O}/{R1}#{A}" not in w, str(w))
    check("pause prio5: не блокирует очередь", w == [ckey], str(w))

    # 6. closed / без метки agent в очередь не попадают
    comment(O, R1, C, "x")
    fj._req("PATCH", f"/repos/{O}/{R1}/issues/{C}", json={"state": "closed"})
    setl(O, R1, B, [])          # сняли метку agent — задача не для агента
    comment(O, R1, B, "y")
    check("closed/no-agent: очередь пуста", poll() == [])

    # 7. метки приоритетов перекрашены под новую семантику (5 = красный)
    fj.ensure_labels(O, R1, list(tasks.PRIORITIES), tasks.LABEL_COLORS)
    col = {l["name"]: l["color"].lstrip("#").lower()
           for l in fj._req("GET", f"/repos/{O}/{R1}/labels").json()}
    check("цвет 5 = красный (d1242f)", col.get("5") == "d1242f", str(col))
    check("цвет 1 = серый (8c959f)", col.get("1") == "8c959f", str(col))
    lid = [l["id"] for l in fj._req("GET", f"/repos/{O}/{R1}/labels").json() if l["name"] == "4"][0]
    fj._req("PATCH", f"/repos/{O}/{R1}/labels/{lid}", json={"color": "abcdef"})
    fj.ensure_labels(O, R1, list(tasks.PRIORITIES), tasks.LABEL_COLORS)
    col = {l["name"]: l["color"].lstrip("#").lower()
           for l in fj._req("GET", f"/repos/{O}/{R1}/labels").json()}
    check("перекраска существующей метки (4 -> fb8500)", col.get("4") == "fb8500", str(col))

    # 8. панель: priorities отдаются по убыванию важности
    view = tasks.config_view(cfg)
    check("панель: priorities = ['5','4','3','2','1']",
          view["priorities"] == ["5", "4", "3", "2", "1"], str(view["priorities"]))
finally:
    for o, r, n in ((O, R1, A), (O, R1, B), (O, R1, C), (O, R1, D), (O, R2, E)):
        try:
            fj._req("PATCH", f"/repos/{o}/{r}/issues/{n}", json={"state": "closed"})
        except Exception:
            pass

bad = [x for x in results if not x[1]]
print("TOTAL", len(results), "FAILS", len(bad))
sys.exit(1 if bad else 0)
