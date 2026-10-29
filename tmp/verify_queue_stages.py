"""Самопроверка идеальной очереди (#queue/#29/#stages) — asserts, без сети и Forgejo.
Запуск: python3 tmp/verify_queue_stages.py (venv agent-loop на CT106 или локально)."""
import sys
sys.path.insert(0, '.')
from agent import bridge as b
from agent import poller as p

now = b._iso_now(0)

# --- _parse_next ---
assert b._parse_next("что-то сделано\nDONE") is None
assert b._parse_next("этап сделан\nNEXT: 120") == 120
assert b._parse_next("NEXT:0") == 1          # clamp снизу
assert b._parse_next("NEXT: 999999") == 43200  # clamp сверху
assert b._parse_next("") is None
assert b._parse_next("NEXT: abc") is None

# --- stage helpers ---
st_future = {"next_wake": b._iso_now(60)}
assert b.stage_waiting(st_future, now) and not b.stage_due(st_future, now)
st_past = {"next_wake": b._iso_now(-1)}
assert b.stage_due(st_past, now) and not b.stage_waiting(st_past, now)
st_none = {}
assert not b.stage_due(st_none, now) and not b.stage_waiting(st_none, now)

# --- _pick_top ---
def mk(key, prio, created, wake=True, incomplete=True, stage_due=False, nextw="", stage=0):
    return {"key": key, "num": int(key.rsplit("#", 1)[1]), "prio": prio,
            "issue": {"created_at": created}, "wake": wake,
            "incomplete": incomplete,
            "hold": incomplete and wake,          # hold = незавершённая И готовая (#29)
            "stage_due": stage_due, "next_wake": nextw,
            "st": {"stage": stage, "ran": wake or incomplete}}

# 1) готовый p5 держит очередь: p4 не проскакивает
rows = [mk("a#1", 5, "2026-01-01"), mk("a#2", 4, "2026-01-02")]
cands, waiting = p._pick_top(rows)
assert cands[0]["key"] == "a#1" and "a#2" not in [c["key"] for c in cands], (cands, waiting)

# 2) p5 в ожидании (кулдаун/этап) очередь НЕ держит -> p4 идёт (#24/#29)
rows = [mk("a#1", 5, "2026-01-01", wake=False), mk("a#2", 4, "2026-01-02")]
cands, waiting = p._pick_top(rows)
assert cands[0]["key"] == "a#2" and waiting[0]["key"] == "a#1"

# 3) done/questions (incomplete=False) очередь не держат, следующая стартует сразу
rows = [mk("a#1", 5, "2026-01-01", wake=False, incomplete=False),
        mk("a#2", 4, "2026-01-02")]
cands, _ = p._pick_top(rows)
assert cands[0]["key"] == "a#2"

# 4) разбуженный этап — раньше высшего приоритета («вторая после текущей»)
rows = [mk("a#1", 5, "2026-01-01"),
        mk("b#2", 3, "2026-01-02", stage_due=True, nextw=b._iso_now(-5), stage=2)]
cands, _ = p._pick_top(rows)
assert cands[0]["key"] == "b#2", cands
# два этапа — по плану раньше
rows = [mk("b#2", 3, "2026-01-02", stage_due=True, nextw=b._iso_now(-30)),
        mk("b#3", 3, "2026-01-03", stage_due=True, nextw=b._iso_now(-10))]
cands, _ = p._pick_top(rows)
assert cands[0]["key"] == "b#2"

# 5) равный приоритет — старее первой
rows = [mk("a#1", 4, "2026-05-01"), mk("a#2", 4, "2026-01-01")]
cands, _ = p._pick_top(rows)
assert cands[0]["key"] == "a#2"

# 6) этап в будущем (waiting) — не будится и не держит: p4 идёт
rows = [mk("a#1", 5, "2026-01-01", wake=False, nextw=b._iso_now(120)),
        mk("a#2", 4, "2026-01-02")]
cands, waiting = p._pick_top(rows)
assert cands[0]["key"] == "a#2" and waiting[0]["key"] == "a#1"

print("OK: ideal queue + stages selfcheck")
