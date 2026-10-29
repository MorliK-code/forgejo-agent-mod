#!/bin/bash
# #28: дождаться конца живого wake (не убить заказчика посреди задачи), затем
# тестовое окно харнеса: overlay (watchdog 90с, чат-гейт выкл) -> стоп agent-loop
# (ни один живой wake не мешает run.json/очереди) -> e2e28_real -> восстановить
# overlay -> старт agent-loop. trap гарантирует восстановление даже при SIGKILL юнита.
restore() {
  if [ -f /var/backups/overlay28.orig ]; then cp /var/backups/overlay28.orig /opt/agent-loop/config.yaml.overlay.yaml; else rm -f /opt/agent-loop/config.yaml.overlay.yaml; fi
  systemctl start agent-loop 2>/dev/null
}
trap restore EXIT
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1)); [ "$n" -gt 360 ] && break      # потолок 60 мин: дальше идём в любом случае
  sleep 10
done
cp /opt/agent-loop/config.yaml.overlay.yaml /var/backups/overlay28.orig 2>/dev/null
cat > /opt/agent-loop/config.yaml.overlay.yaml <<'EOF'
forgejo:
  paused: false
bridge:
  main_chat_idle_gate_s: 0
  watchdog_idle_s: 90
EOF
systemctl stop agent-loop
logger -t agent-28 "harness window: agent-loop stopped, watchdog_idle_s=90, gate off"
/opt/agent-loop/venv/bin/python /opt/agent-loop/repo/tmp/e2e28_real.py > /var/log/e2e28_real.out 2>&1
r=$?
logger -t agent-28 "e2e28 rc=$r; overlay restored; agent-loop started"
exit $r
