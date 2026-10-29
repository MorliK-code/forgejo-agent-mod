#!/bin/bash
# #28: деплой watchdog-наблюдателя. Харнес-файлы уже в репо (пушнуты агентом #28).
# Порядок: pull -> оффлайн-проверка на задеплоенном коде -> watchdog_idle_s в живой
# конфиг (default 600) -> рестарт agent-admin (наблюдатель с watchdog; agent-loop НЕ
# трогаем — живой wake заказчика доживает) -> отложенный reload28 (харнес после конца wake).
cd /opt/agent-loop/repo || exit 1
git pull --ff-only || { echo "PULL FAILED"; exit 1; }
P=/opt/agent-loop/venv/bin/python
for f in agent/bridge.py agent/poller.py agent/admin_server.py; do
  $P -m py_compile "$f" || { echo "SYNTAX FAIL $f"; exit 1; }
done
$P tmp/verify_watchdog.py > /tmp/e2e28_verify.out 2>&1; r1=$?
echo "verify rc=$r1"; tail -3 /tmp/e2e28_verify.out
if [ $r1 -ne 0 ]; then echo "TESTS FAILED"; exit 1; fi
grep -q watchdog_idle_s /opt/agent-loop/config.yaml || \
  sed -i '/main_chat_idle_gate_s/a\  watchdog_idle_s: 600    # #28: тишина work-сессии -> стоп прогона' /opt/agent-loop/config.yaml
systemctl restart agent-admin
systemd-run --unit=restart-agent-28 --collect --property=RuntimeMaxSec=3000 /bin/bash /opt/agent-loop/repo/tmp/reload28.sh
echo "deploy28 scheduled (reload28 waits for live wake)"
