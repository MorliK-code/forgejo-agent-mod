#!/bin/bash
# #25: перезапуск после правки поллера/бэкенда — отложенно, когда ни один wake
# не в полёте (не убиваем живой Hermes-прогон посреди задачи).
n=0
while pgrep -f "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new user@192.168.110.4" >/dev/null 2>&1; do
  n=$((n+1))
  [ "$n" -gt 360 ] && break      # потолок 60 мин
  sleep 10
done
systemctl restart agent-loop agent-admin
logger -t agent-reload25 "agent-loop+agent-admin перезапущены (пауза очереди #25)"
