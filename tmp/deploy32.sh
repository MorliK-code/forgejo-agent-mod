#!/bin/sh
# deploy32 — #32: WORK_PROMPT (agent/bridge.py) грузится полкером при импорте,
# поэтому live-изменение = рестарт agent-loop. Рестарт лупа на середине wake-прогона
# недопустим: atexit poller = pause_at_exit -> pkill живых agent-прогонов на 110.4.
# Ждём, пока все .work-*.lock на 110.4 свободны (нет активного прогона), затем
# pull + проверка WORK_PROMPT + рестарт. Гонка с новым wake (15с тик полкера)
# само-лечится через #31 (last_err -> повторный wake).
LOG=/var/log/deploy32.log
echo "$(date -Is) deploy32: start" >>"$LOG"
while ! ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4 \
    'for f in /home/user/agent-work/.work-*.lock; do [ -e "$f" ] || continue; flock -n "$f" true || exit 1; done' 2>>"$LOG"; do
  sleep 5
done
echo "$(date -Is) deploy32: all agent locks free" >>"$LOG"
cd /opt/agent-loop/repo || { echo "$(date -Is) deploy32: no repo" >>"$LOG"; exit 1; }
git pull --ff-only >>"$LOG" 2>&1 || { echo "$(date -Is) deploy32: pull FAILED" >>"$LOG"; exit 1; }
/opt/agent-loop/venv/bin/python - >>"$LOG" 2>&1 <<'EOF'
import sys
sys.path.insert(0, "/opt/agent-loop/repo")
from agent.bridge import WORK_PROMPT
assert "Критерии готовности" in WORK_PROMPT, "WORK_PROMPT без правила чек-листа"
print("WORK_PROMPT checklist rule present")
EOF
[ $? -eq 0 ] || { echo "$(date -Is) deploy32: WORK_PROMPT check FAILED" >>"$LOG"; exit 1; }
systemctl restart agent-loop >>"$LOG" 2>&1
echo "$(date -Is) deploy32: agent-loop restarted, WORK_PROMPT live" >>"$LOG"
