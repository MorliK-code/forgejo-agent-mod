#!/bin/bash
# #24: перезапуск agent-loop с гейтом parked-приоритетов — отложенно, когда ни один
# wake не в полёте (чтобы не убить живой Hermes-прогон посреди задачи).
# Только agent-loop: agent-admin не трогаем (сбрасывает fmt-потоки/лимитер).
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1))
  [ "$n" -gt 360 ] && break      # потолок 60 мин: дальше перезапускаем в любом случае
  sleep 10
done
systemctl restart agent-loop
logger -t agent-reload24 "agent-loop перезапущен (гейт приоритетов #24)"
