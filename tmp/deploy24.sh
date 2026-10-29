#!/bin/bash
# #24: прогон харнесов на задеплоенном коде; при успех — отложенный рестарт agent-loop.
cd /opt/agent-loop/repo
P=/opt/agent-loop/venv/bin/python
$P tmp/e2e24_harness.py > /tmp/e2e24.out 2>&1; r24=$?
$P tmp/e2e19_harness.py > /tmp/e2e19.out 2>&1; r19=$?
echo "e2e24 rc=$r24 e2e19 rc=$r19"
echo "---- e2e24 tail:"; tail -8 /tmp/e2e24.out
echo "---- e2e24 gate-logs:"; grep -c "^PASS" /tmp/e2e24.out; grep "держат\|не стартуют" /tmp/e2e24.out | head -8
echo "---- e2e19 tail:"; tail -3 /tmp/e2e19.out
if [ $r24 -eq 0 ] && [ $r19 -eq 0 ]; then
  systemd-run --unit=restart-agent-24 --collect /bin/bash /opt/agent-loop/repo/tmp/reload24.sh && echo "restart scheduled"
else
  echo "RESTART NOT SCHEDULED"
fi
