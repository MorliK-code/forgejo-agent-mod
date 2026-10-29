#!/bin/bash
# #31: дождаться конца живого wake (не убить прогон заказчика), затем: стоп agent-loop ->
# живой харнес e2e31 (песочница hermes/agent-e2e31, реальный Forgejo+ssh 110.4) ->
# старт agent-loop (уже на новом коде last_err). trap восстанавливает юнит при любой гибели.
restore() { systemctl start agent-loop 2>/dev/null; }
trap restore EXIT
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1)); [ "$n" -gt 360 ] && break      # потолок 60 мин: дальше идём в любом случае
  sleep 10
done
systemctl stop agent-loop
logger -t agent-31 "reload31: agent-loop остановлен (живой wake закончен), запуск харнеса e2e31"
/opt/agent-loop/venv/bin/python /opt/agent-loop/repo/tmp/e2e31_harness.py > /var/log/e2e31.out 2>&1
r=$?
systemctl start agent-loop
logger -t agent-31 "e2e31 rc=$r; agent-loop перезапущен на новом коде (#31 last_err)"
exit $r
