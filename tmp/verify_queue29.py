"""Регресс #29 (оффлайн, без сети и Forgejo): hold-логика _bridge_candidates + _pick_top.

Правила заказчика (#29): задача на done/pause/questions НЕ держит очередь —
переход к следующей происходит сразу (в том же цикле). Тест-кейсы заказчика:
p5(done)+p4 -> p4 стартует; p5(questions)+p3 -> p3 стартует. Плюс: pause вне
очереди вообще; припаркованная (ran=True) без меток — механика кулдауна (#24)
и очередь не блокирует.

Запуск: python3 tmp/verify_queue29.py (локально или venv agent-loop на CT106)."""
import sys
sys.path.insert(0, '.')
from datetime import datetime, timedelta, timezone

from agent import poller as p
from agent import tasks as tasks_mod


def iso(minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class FakeFJ:
    def __init__(self, issues):  # num -> {"labels": [...], "comments": [...]}
        self.issues_map = issues

    def issues(self, owner, repo, state="open"):
        return [dict(number=n, title=f"t{n}", body="", created_at=f"2026-01-0{n}T00:00:00Z",
                     labels=[{"name": l} for l in d["labels"]])
                for n, d in sorted(self.issues_map.items())]

    def issue_comments(self, owner, repo, number):
        return self.issues_map[number].get("comments", [])


tasks_mod.targets = lambda cfg: [("t", "r")]
CFG = {"forgejo": {"trigger_labels": ["agent"], "checkbox": "Агент",
                   "owner": "t", "repo": "r"},
       "bridge": {"parked_retry_s": 1800, "main_chat_idle_gate_s": 0}}


def cust_comment(mins=-60, at=None):
    upd = at or iso(mins)
    return [{"id": 1, "user": {"login": "morlik"}, "body": "ok",
             "created_at": upd, "updated_at": upd}]


def ran_state(last_cid=0, last_upd="", last_wake=None):
    return {"last_cid": last_cid, "last_upd": last_upd, "ran": True,
            "last_wake": last_wake if last_wake is not None else iso(0)}


def rows_of(issues, bs=None):
    bs = bs if bs is not None else {}
    rows = p._bridge_candidates(CFG, FakeFJ(issues), bs, ["agent"], "Агент", "hermes")
    return {r["key"]: r for r in rows}, p._pick_top(rows), bs


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        sys.exit(f"FAIL: {name}")


# --- кейс 1 заказчика: p5(done) + p4 -> p4 стартует в том же цикле ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "done"]},
                               2: {"labels": ["agent", "4"]}})
check("p5 done: не будится и не держит (первый захват её не трогает)",
      not rows["t/r#1"]["wake"] and not rows["t/r#1"]["hold"])
check("p5 done + p4: p4 стартует сразу", cands[0]["key"] == "t/r#2"
      and "t/r#1" not in [c["key"] for c in cands])

# --- кейс 2 заказчика: p5(questions) + p3 -> p3 стартует ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "questions", "in-progress"]},
                               2: {"labels": ["agent", "3"]}})
check("p5 questions + p3: p3 стартует, questions отпущена",
      cands[0]["key"] == "t/r#2" and not rows["t/r#1"]["hold"]
      and not rows["t/r#1"]["wake"])

# --- pause: задача вне очереди вообще, даже со свежим комментарием ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "pause"], "comments": cust_comment()},
                               2: {"labels": ["agent", "4"]}})
check("p5 pause: вообще вне очереди (коммент не будит), p4 идёт",
      "t/r#1" not in rows and cands[0]["key"] == "t/r#2")

# --- припаркованная (ran=True) без меток состояния: кулдаун, очередь НЕ блокирует ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "in-progress"]},
                               2: {"labels": ["agent", "4"]}},
                              bs={"t/r#1": ran_state()})
check("parked p5: очередь не держит, p4 стартует (#24/#29)",
      cands[0]["key"] == "t/r#2" and not rows["t/r#1"]["wake"])

# --- механика кулдауна: просроченный parked сам будится (не навечно вне игры) ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "in-progress"]},
                               2: {"labels": ["agent", "4"]}},
                              bs={"t/r#1": ran_state(last_wake=iso(-120))})
check("parked p5 с истёкшим кулдауном: будится сам и держит p4",
      rows["t/r#1"]["wake"] and rows["t/r#1"]["hold"] and cands[0]["key"] == "t/r#1")

# --- questions игнорирует кулдаун (ждёт комментарий заказчика, не авто-retry) ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "questions", "in-progress"]},
                               2: {"labels": ["agent", "3"]}},
                              bs={"t/r#1": ran_state(last_wake=iso(-120))})
check("questions с просроченным last_wake: не авто-будится",
      not rows["t/r#1"]["wake"] and cands[0]["key"] == "t/r#2")

# --- возобновление (#7): свежий коммент к done p5 будит её сквозь p4-холд ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "done"], "comments": cust_comment()},
                               2: {"labels": ["agent", "4"]}})
check("done p5 + свежий коммент: возобновляется первой (prio5 > холд p4)",
      rows["t/r#1"]["wake"] and not rows["t/r#1"]["hold"] and cands[0]["key"] == "t/r#1")

# --- тот же коммент уже потреблён прогоном: повторного wake нет, очередная стартует ---
upd = iso(-60)
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "done"], "comments": cust_comment(at=upd)},
                               2: {"labels": ["agent", "4"]}},
                              bs={"t/r#1": ran_state(last_cid=1, last_upd=upd)})
check("коммент done уже потреблён: повторного wake нет, p4 стартует",
      not rows["t/r#1"]["wake"] and cands[0]["key"] == "t/r#2")

# --- этап: waiting (NEXT в будущем) не держит; срок этапа — первым, gate не мешает ---
rows, (cands, waiting), _ = rows_of({1: {"labels": ["agent", "5", "in-progress"]},
                                     2: {"labels": ["agent", "4"]}},
                                    bs={"t/r#1": {**ran_state(), "next_wake": iso(120)}})
check("этап в будущем: p5 не будится/не держит, p4 идёт",
      not rows["t/r#1"]["wake"] and cands[0]["key"] == "t/r#2"
      and waiting and waiting[0]["key"] == "t/r#1")
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "in-progress"]},
                               2: {"labels": ["agent", "4"]}},
                              bs={"t/r#1": {**ran_state(), "next_wake": iso(-5)}})
check("срок этапа p5: идёт первым (stage_due), p4 ждёт",
      rows["t/r#1"]["wake"] and rows["t/r#1"]["stage_due"] and cands[0]["key"] == "t/r#1")

# --- done с запланированным этапом (агент сам так запланировал): будится, но НЕ держит ---
rows, (cands, _), _ = rows_of({1: {"labels": ["agent", "5", "done"]},
                               2: {"labels": ["agent", "4"]}},
                              bs={"t/r#1": {**ran_state(), "next_wake": iso(-5)}})
check("done с просроченным этапом: wake по плану агента, hold всё равно снят",
      rows["t/r#1"]["wake"] and not rows["t/r#1"]["hold"])

print("OK: #29 hold-логика (done/questions/pause отпускают очередь немедленно)")
