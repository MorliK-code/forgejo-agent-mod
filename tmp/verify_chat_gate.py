"""Самопроверка чат-гейта (#chat-gate) и свипа сирот — asserts, без сети."""
import sys
import time
sys.path.insert(0, '.')
from agent import bridge as b

# _parse_idle: epoch / ISO / мусор
now = time.time()
assert 119 < b._parse_idle(str(now - 120)) <= 121, "epoch idle"
assert b._parse_idle("") is None
assert b._parse_idle("not-a-time") is None
assert b._parse_idle("2026-10-25T00:00:00+03:00") >= 0, "ISO idle"

# chat_gate_active: idle<gate -> гейт; idle>=gate -> нет; None -> fail-open; gate<=0 -> выключен
orig = b.main_chat_idle
try:
    cfg = {"bridge": {"main_chat_idle_gate_s": 180}}
    b.main_chat_idle = lambda c: 120.0
    assert b.chat_gate_active(cfg) is True, "chat active -> hold"
    b.main_chat_idle = lambda c: 200.0
    assert b.chat_gate_active(cfg) is False, "idle past gate -> run"
    b.main_chat_idle = lambda c: None
    assert b.chat_gate_active(cfg) is False, "no 110.4 -> fail-open"
    b.main_chat_idle = lambda c: 5.0
    assert b.chat_gate_active({"bridge": {"main_chat_idle_gate_s": 0}}) is False, "gate off"
    assert b.chat_gate_active({}) is True  # нет bridge-блока -> default gate 180, не падает
finally:
    b.main_chat_idle = orig

# sweep_agent_runs: отсутствие мостового конфига не роняет (ssh упадёт -> False)
assert b.sweep_agent_runs({"bridge": {"host": "192.0.2.1", "user": "nobody"}}) is False

print("OK: chat-gate + sweep selfcheck")
