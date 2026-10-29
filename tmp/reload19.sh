#!/bin/bash
# #19: перезапуск агент-лупа с новой очередью приоритетов — отложенно, когда ни один
# wake не в полёте (чтобы не убить живой Hermes-прогон посреди задачи).
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1))
  [ "$n" -gt 120 ] && break      # потолок 20 мин: дальше перезапускаем в любом случае
  sleep 10
done
systemctl restart agent-loop agent-admin
logger -t agent-reload19 "agent-loop/agent-admin перезапущены (очередь приоритетов #19)"
