#!/bin/bash
# #29: перезапуск agent-loop с новой hold-логикой очереди — отложенно, когда ни один
# wake не в полёте (чтобы не убить живой Hermes-прогон посреди задачи).
# Только agent-loop: agent-admin не трогаем.
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1))
  [ "$n" -gt 360 ] && break      # потолок 60 мин: дальше перезапускаем в любом случае
  sleep 10
done
systemctl restart agent-loop
logger -t agent-reload29 "agent-loop перезапущен (hold-логика done/questions/pause #29)"
