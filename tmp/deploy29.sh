#!/bin/bash
# #29: прогон регресса очереди на задеплоенном коде; при успех — реальный wake-харнес
# и отложенный рестарт agent-loop (reload29 ждёт, пока живой wake не завершится).
cd /opt/agent-loop/repo || exit 1
P=/opt/agent-loop/venv/bin/python
$P tmp/verify_queue29.py > /tmp/e2e29_verify.out 2>&1; r1=$?
$P tmp/verify_queue_stages.py > /tmp/e2e29_stages.out 2>&1; r2=$?
$P tmp/e2e29_harness.py > /tmp/e2e29_h.out 2>&1; r3=$?
echo "verify29 rc=$r1 stages rc=$r2 harness rc=$r3"
echo "---- verify tail:"; tail -4 /tmp/e2e29_verify.out
echo "---- harness:"; grep -E "^(PASS|FAIL|TOTAL)" /tmp/e2e29_h.out
if [ $r1 -eq 0 ] && [ $r2 -eq 0 ] && [ $r3 -eq 0 ]; then
  systemd-run --unit=e2e29-real --collect --setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -c "/opt/agent-loop/venv/bin/python /opt/agent-loop/repo/tmp/e2e29_real.py > /tmp/e2e29_real.out 2>&1"
  echo "real harness started"
  systemd-run --unit=restart-agent-29 --collect /bin/bash /opt/agent-loop/repo/tmp/reload29.sh
  echo "restart scheduled"
else
  echo "TESTS FAILED"
fi
