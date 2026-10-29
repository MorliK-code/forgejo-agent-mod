#!/bin/bash
cd /opt/agent-loop/repo
git pull --ff-only 2>&1 | tail -1
systemctl restart agent-admin
sleep 1
echo "=== services ==="
systemctl is-active forgejo agent-admin agent-proxy agent-loop
echo "=== bootstrap: active mods ==="
curl -s http://127.0.0.1:8080/__mods__/bootstrap.js | grep -o 'agent-[a-z]*' | sort -u
echo "=== statusbar asset reachable ==="
curl -s -o /dev/null -w "statusbar main.js -> %{http_code}\n" http://127.0.0.1:8080/__mods__/mods/agent-statusbar/main.js
echo "=== enable statusbar via API, check bootstrap, disable back ==="
PW=$(grep ^AGENT_ADMIN_PASSWORD= /opt/agent-loop/env | cut -d= -f2)
curl -s -c /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/login -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" >/dev/null
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/enable -H "Content-Type: application/json" -d '{"name":"agent-statusbar","enabled":true}'
echo
echo "bootstrap now:"
curl -s http://127.0.0.1:8080/__mods__/bootstrap.js | grep -o 'agent-[a-z]*' | sort -u
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/enable -H "Content-Type: application/json" -d '{"name":"agent-statusbar","enabled":false}' >/dev/null
rm -f /tmp/a
echo "=== final bootstrap ==="
curl -s http://127.0.0.1:8080/__mods__/bootstrap.js | grep -o 'agent-[a-z]*' | sort -u
echo "=== state API: mods section ==="
curl -s -c /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/login -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" >/dev/null
curl -s -b /tmp/a http://127.0.0.1:8091/agent-admin/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print('service:', d['service']); print('mods:', [(m['dir'], m['enabled']) for m in d['mods']])"
rm -f /tmp/a
