#!/bin/bash
set -e
cd /opt/agent-loop/repo
git pull --ff-only 2>&1 | tail -1
systemctl restart agent-admin
sleep 1
PW=$(grep ^AGENT_ADMIN_PASSWORD= /opt/agent-loop/env | cut -d= -f2)
curl -s -c /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/login -H "Content-Type: application/json" -d "{\"password\":\"$PW\"}" >/dev/null
echo "=== redeploy (overwrite our files) ==="
curl -s -b /tmp/a -X POST http://127.0.0.1:8091/agent-admin/api/mods/deploy -H "Content-Type: application/json" -d '{"name":"agent-settings"}'
echo
sleep 3
systemctl is-active forgejo
echo "=== assets ==="
curl -s -o /dev/null -w "js -> %{http_code} size=%{size_download}\n" http://127.0.0.1:8080/assets/agent-settings/agent-settings.js
echo "=== navbar rendered (via login session) ==="
su git -c "/usr/local/bin/forgejo admin user create --username verifyagent --email va@local --password ut4JWOrZJlu_ --must-change-password=false --config /home/git/forgejo/custom/conf/app.ini" 2>&1 | grep -i "created\|error" | head -1
cd /tmp && rm -f cjs.txt
curl -s -c cjs.txt http://127.0.0.1:3000/user/login -o /dev/null
curl -s -b cjs.txt -c cjs.txt -X POST http://127.0.0.1:3000/user/login --data-urlencode "user_name=verifyagent" --data-urlencode "password=ut4JWOrZJlu_" -o /dev/null
curl -s -b cjs.txt -L http://127.0.0.1:3000/user/settings -o s4.html -w "settings: %{http_code}\n"
echo "agent menu: $(grep -c 'settings#agent' s4.html), mods menu: $(grep -c 'settings#mods' s4.html)"
su git -c "/usr/local/bin/forgejo admin user delete -u verifyagent --config /home/git/forgejo/custom/conf/app.ini" 2>&1 | grep -i "deleted\|error" | head -1
rm -f /tmp/a /tmp/cjs.txt
echo done
