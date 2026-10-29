#!/bin/bash
# деплой mod-системы v2: нативные template-оверраи
# - pull репо на CT
# - рестарт agent-admin (новый код: deploy/undeploy API)
# - deploy мода agent-settings (templates/ + public/ в $FORGEJO_CUSTOM + рестарт forgejo)
set -e
cd /opt/agent-loop/repo
git pull --ff-only 2>&1 | tail -1

# nginx: новый конфиг (без sub_filter)
cp /opt/agent-loop/repo/deploy/agent-proxy.conf /etc/agent-proxy/agent-proxy.conf
nginx -c /etc/agent-proxy/agent-proxy.conf -t 2>&1 | tail -1
systemctl reload agent-proxy || systemctl restart agent-proxy

systemctl restart agent-admin
sleep 1

# deploy мода через API (тот же код, что кнопка в панели)
PW=$(grep ^AGENT_ADMIN_PASSWORD= /opt/agent-loop/env | cut -d= -f2)
curl -s -c /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/login -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" >/dev/null
echo "=== deploy agent-settings ==="
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/deploy -H "Content-Type: application/json" -d '{"name":"agent-settings"}'
echo
sleep 3
echo "=== forgejo active? ==="
systemctl is-active forgejo
echo "=== custom/ contents ==="
find /home/git/forgejo/custom -type f | sort
echo "=== assets served by forgejo ==="
curl -s -o /dev/null -w "agent-settings.js -> %{http_code}\n" http://127.0.0.1:8080/assets/agent-settings/agent-settings.js
curl -s -o /dev/null -w "agent-settings.css -> %{http_code}\n" http://127.0.0.1:8080/assets/agent-settings/agent-settings.css
echo "=== settings page (via proxy, no auth) contains menu item? ==="
curl -s http://127.0.0.1:8080/user/login | grep -c "agent-settings.js" || true
rm -f /tmp/a
