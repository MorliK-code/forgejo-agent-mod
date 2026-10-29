"""e2e-свип правил задачи #17 (poll_once_bridge) против живого Forgejo.
Без реальных LLM-гонок: work_task = stub. Тестовый issue — в отдельном
неполлируемом репо hermes/agent-e2e17 (живой поллер его не видит).
Токен/секреты не печатаются.
"""
import sys
sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient
from agent import bridge, poller, tasks

cfg = load_live("/opt/agent-loop/config.yaml")
cfg["__path__"] = "/tmp/e2e17/config.yaml"  # state-файл теста — изолирован
import os
os.makedirs("/tmp/e2e17", exist_ok=True)
O, R = "hermes", "agent-e2e17"              # неполлируемый песочничный репо
fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
cfg["forgejo"]["owner"], cfg["forgejo"]["repo"] = O, R
U0 = cfg["forgejo"]["user"]
cfg["forgejo"]["user"] = "e2e-customer-fake"  # комментарии hermes-токена считаются «клиентскими»

try:
    fj._req("POST", "/user/repos", json={"name": R, "private": False})
except Exception as e:
    print("repo exists?", str(e)[:120])

calls = []
def fake_work(cfg, fj, owner, repo, num, body, fresh, first):
    calls.append({"num": num, "first": first, "fresh": len(fresh)})
    return 0, "DONE"
bridge.work_task = fake_work

issue = fj.create_issue(O, R, "e2e-тест #17 (одноразовый)",
                        "Одноразовый тест поллера правил меток #17. Игнорировать.")
N = issue["number"]
N2 = 0  # создаётся в секции 10; finally безопасен при преждевременном падении
NUMS = {N}
_orig_issues = fj.issues
fj.issues = lambda owner, repo, state="open": [i for i in _orig_issues(owner, repo, state)
                                               if (owner, repo) == (O, R) and i["number"] in NUMS]

def labels():
    return sorted(fj.issue_labels(O, R, N))
def poll(state=None):
    st = state if state is not None else {"done": []}
    poller.poll_once_bridge(cfg, fj, st)
    return st

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)

def comment(t):
    fj.create_comment(O, R, N, t)

try:
    # 0. нет триггера — не будим
    poll()
    check("no-trigger: нет пробуждения", len(calls) == 0)

    # 1. pause блокирует wake, in-progress не ставится
    fj.set_labels(O, R, N, ["agent", "pause", "2"])
    comment("e2e c1")
    poll()
    check("pause: нет пробуждения", len(calls) == 0)
    check("pause: in-progress не навешан", "in-progress" not in labels())

    # 2. снятие паузы -> wake (first), приоритет 2 сохранён
    fj.set_labels(O, R, N, ["agent", "2"])
    state = poll()
    check("resume: wake first=True fresh=1",
          calls == [{"num": N, "first": True, "fresh": 1}], str(calls))
    check("resume: метки = приоритет+agent+in-progress",
          labels() == sorted(["agent", "2", "in-progress"]), str(labels()))

    # 3. идемпотентность + липкий in-progress после успешного прогона
    poll(state)
    check("idle: нет повторного wake", len(calls) == 1)
    check("sticky: in-progress НЕ снят поллером", "in-progress" in labels())

    # 4. новый комментарий -> wake (не first), in-progress остаётся
    comment("e2e c2")
    state = poll(state)
    check("comment: wake first=False fresh=1",
          calls[-1] == {"num": N, "first": False, "fresh": 1}, str(calls))

    # 5. done+questions без комментариев — не будим; приоритет не трогается
    fj.set_labels(O, R, N, ["agent", "2", "done", "questions"])
    poll(state)
    check("done без комментариев: нет wake", len(calls) == 2)
    check("done: поллер не тронул метки", labels() == sorted(["agent", "2", "done", "questions"]))

    # 6. комментарий к done-задаче -> повторный wake, done/questions сняты, in-progress вернулся
    comment("e2e c3")
    state = poll(state)
    check("done+wake: пробуждение есть", len(calls) == 3 and calls[-1]["first"] is False)
    check("done+wake: done/questions сняты, in-progress+приоритет+agent на месте",
          labels() == sorted(["agent", "2", "in-progress"]), str(labels()))

    # 7. пауза поверх работы блокирует; после снятия — накопленный комментарий приходит
    fj.set_labels(O, R, N, ["agent", "2", "in-progress", "pause"])
    comment("e2e c4")
    poll(state)
    check("pause в работе: нет wake", len(calls) == 3)
    fj.set_labels(O, R, N, ["agent", "2", "in-progress"])
    state = poll(state)
    check("pause снята: накопленный fresh=1", len(calls) == 4 and calls[-1]["fresh"] == 1)

    # 8. метки pause/questions созданы в репо (ensure_labels)
    names = [l["name"] for l in fj._req("GET", f"/repos/{O}/{R}/labels").json()]
    check("репо: метки pause/questions существуют", "pause" in names and "questions" in names)

    # 9. закрытая задача не поллится
    fj._req("PATCH", f"/repos/{O}/{R}/issues/{N}", json={"state": "closed"})
    poll(state)
    check("closed: нет wake", len(calls) == 4)

    # 10. tag_task (панель): priority=None сохраняет приоритет; '2' — заменяет один
    N2 = fj.create_issue(O, R, "e2e-тест #17b (tag_task)", "Одноразовый тест. ")["number"]
    NUMS.add(N2)
    fj.set_labels(O, R, N2, ["1", "agent"])
    u = cfg["forgejo"]["user"]; cfg["forgejo"]["user"] = U0  # client(cfg) — реальный логин
    try:
        tasks.tag_task(cfg, f"{O}/{R}", N2, priority=None, agent=None)
        check("tag_task(None): приоритет 1 и agent сохранены",
              sorted(fj.issue_labels(O, R, N2)) == sorted(["1", "agent"]),
              str(fj.issue_labels(O, R, N2)))
        tasks.tag_task(cfg, f"{O}/{R}", N2, priority="2", agent=None)
        check("tag_task('2'): приоритет заменён, дублей нет",
              sorted(fj.issue_labels(O, R, N2)) == sorted(["2", "agent"]),
              str(fj.issue_labels(O, R, N2)))
    finally:
        cfg["forgejo"]["user"] = u

    # 11. дедуп приоритетов на wake: два приоритета -> остаётся наименьший
    fj.set_labels(O, R, N2, ["1", "2", "agent"])
    fj.create_comment(O, R, N2, "e2e c5")
    state = poll(state)
    check("dedup: wake c fresh=1", len(calls) == 5 and calls[-1]["num"] == N2)
    check("dedup: два приоритета -> один (наименьший)",
          sorted(fj.issue_labels(O, R, N2)) == sorted(["1", "agent", "in-progress"]),
          str(fj.issue_labels(O, R, N2)))
finally:
    for n in (N, N2):
        try:
            fj._req("PATCH", f"/repos/{O}/{R}/issues/{n}", json={"state": "closed"})
        except Exception:
            pass

bad = [r for r in results if not r[1]]
print("TOTAL", len(results), "FAILS", len(bad))
sys.exit(1 if bad else 0)
