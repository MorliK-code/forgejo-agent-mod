"""Самопроверка watchdog живого прогона (#28): asserts, без сети."""
import os
import sys
import tempfile
import time
import types
os.environ["AGENT_RUN_FILE"] = os.path.join(tempfile.mkdtemp(), "run.json")  # STOP_MARK -> tmp
sys.path.insert(0, '.')
from agent import bridge as b

now = time.time()

# charset-guard: кривое/пустое имя сессии -> None, ssh не запускается
_orig_run = b.subprocess.run
called = []
b.subprocess.run = lambda *a, **k: (called.append(1), types.SimpleNamespace(stdout=""))[1]
try:
    assert b.work_session_idle({}, "bad';name") is None
    assert b.work_session_idle({}, "") is None
    assert not called, "ssh не дёргается на мусорном имени"

    # fail-open: сессии нет в БД (пустой вывод) -> None
    b.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="")
    assert b.work_session_idle({}, "work-x") is None, "нет сессии/связи -> None (fail-open)"

    # since-гард: свежий wake молчит от старта прогона, а не от старого wake
    b.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=str(now - 500))
    idle = b.work_session_idle({}, "work-x", str(now - 10))  # since epoch 10с назад
    assert idle is not None and 9 <= idle <= 12, f"тишина считается с max(посл.сообщение, старт) -> {idle}"
    idle = b.work_session_idle({}, "work-x", "")  # без старта: по последнему сообщению
    assert idle is not None and 499 <= idle <= 502, f"без since -> по сообщению -> {idle}"

    # watchdog_idle: порог/выключен/fail-open
    cfg90 = {"bridge": {"watchdog_idle_s": 90}}
    b.work_session_idle = lambda c, s, since="": None
    assert b.watchdog_idle(cfg90, "work-x") is None, "нет связи -> не убивать"
    b.work_session_idle = lambda c, s, since="": 88.0
    assert b.watchdog_idle(cfg90, "work-x") is None, "ниже порога -> жив"
    b.work_session_idle = lambda c, s, since="": 91.0
    assert b.watchdog_idle(cfg90, "work-x") == 91.0, "выше порога -> сигнал"
    b.work_session_idle = lambda c, s, since="": 9999.0
    assert b.watchdog_idle({"bridge": {"watchdog_idle_s": 0}}, "work-x") is None, "порог 0 -> выключен"
    assert b.watchdog_idle(cfg90, "") is None, "пустая сессия -> None"
    b.work_session_idle = lambda c, s, since="": 601.0
    assert b.watchdog_idle({}, "work-x") == 601.0, "default порог 600"
    b.work_session_idle = lambda c, s, since="": 599.0
    assert b.watchdog_idle({"bridge": {}}, "work-x") is None, "599 < 600 -> жив"
finally:
    b.subprocess.run, b.work_session_idle = _orig_run, b.work_session_idle

# RUN_FILE прогона содержит started (#28: отсчёт тишины наблюдателем)
import inspect
src = inspect.getsource(b.work_task)
assert '"started": _iso_now()' in src, "run.json должен несть started"

# отметка kill (#28): убитый TERM'ом hermes дописывает сообщение и тишина на учёте
# не видна — полкер обязан верифицировать факт kill отметкой наблюдателя
b.mark_watchdog_stop("k/1")
assert b.watchdog_stop_marked("k/1") is True, "отметка получена"
assert b.watchdog_stop_marked("k/1") is False, "отметка одноразовая"
b.mark_watchdog_stop("k/2")
assert b.watchdog_stop_marked("k/1") is False, "чужая задача"
assert b.watchdog_stop_marked("k/2") is True, "своя задача"
b.STOP_MARK.write_text('{"key": "k/3", "ts": "2020-01-01T00:00:00+00:00"}')
assert b.watchdog_stop_marked("k/3") is False, "протухшая отметка (>15 мин)"
assert b.watchdog_stop_marked("нет-файла") is False

print("OK: watchdog selfcheck")
