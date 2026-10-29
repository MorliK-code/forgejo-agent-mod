#!/bin/bash
set -e
cd /opt/agent-loop/repo
git pull --ff-only 2>&1 | tail -1
systemctl restart agent-admin
sleep 1
PW=$(grep ^AGENT_ADMIN_PASSWORD= /opt/agent-loop/env | cut -d= -f2)
curl -s -c /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/login -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" >/dev/null
echo "=== undeploy old (wrong location) ==="
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/undeploy -H "Content-Type: application/json" -d '{"name":"agent-settings"}'
echo
# leftover from old deploy (public/agent-settings/ без assets/ prefix) — точечно
if [ -d /home/git/forgejo/custom/public/agent-settings ] && [ ! -d /home/git/forgejo/custom/public/assets ]; then
  rm -rf /home/git/forgejo/custom/public/agent-settings
fi
echo "=== deploy new ==="
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/deploy -H "Content-Type: application/json" -d '{"name":"agent-settings"}'
echo
sleep 3
echo "=== forgejo ==="
systemctl is-active forgejo
echo "=== custom files ==="
find /home/git/forgejo/custom -type f | sort
echo "=== assets via direct :3000 ==="
curl -s -o /dev/null -w "agent-settings.js -> %{http_code}\n" http://127.0.0.1:3000/assets/agent-settings/agent-settings.js
curl -s -o /dev/null -w "agent-settings.css -> %{http_code}\n" http://127.0.0.1:3000/assets/agent-settings/agent-settings.css
echo "=== assets via nginx :8080 ==="
curl -s -o /dev/null -w "js -> %{http_code}\n" http://127.0.0.1:8080/assets/agent-settings/agent-settings.js
echo "=== settings navbar has agent link (login page check via :3000 — need auth; check template parse instead) ==="
curl -s http://127.0.0.1:3000/user/login -o /tmp/lp.html -w "login page: %{http_code}\n"
grep -c "agent-settings" /tmp/lp.html || echo "(not on login page — expected, it's on settings)"
rm -f /tmp/a
