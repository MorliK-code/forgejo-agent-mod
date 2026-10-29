#!/bin/bash
set -e
cd /opt/agent-loop/repo
git pull --ff-only 2>&1 | tail -1
# убрать старый пароль из env (больше не нужен)
sed -i '/^AGENT_ADMIN_PASSWORD=/d' /opt/agent-loop/env
systemctl restart agent-admin
sleep 1
systemctl is-active agent-admin
echo "=== state без сессии (ждём 401) ==="
curl -s -o /dev/null -w "no-cookie -> %{http_code}\n" http://127.0.0.1:8091/agent-admin/api/state
echo "=== redeploy мода (новые js) ==="
# логинимся в forgejo, чтобы получить cookie сессии
cd /tmp && rm -f cjs.txt
curl -s -c cjs.txt http://127.0.0.1:3000/user/login -o /dev/null
curl -s -b cjs.txt -c cjs.txt -X POST http://127.0.0.1:3000/user/login --data-urlencode "user_name=e2eagent" --data-urlencode "password=e2eTmp_Pw1" -o /dev/null -w "login post: %{http_code}\n"
echo "state с сессией:"
curl -s -b cjs.txt http://127.0.0.1:8091/agent-admin/api/state | head -c 200; echo
curl -s -b cjs.txt -X POST http://127.0.0.1:8091/agent-admin/api/mods/deploy -H "Content-Type: application/json" -d '{"name":"agent-settings"}'; echo
sleep 3
systemctl is-active forgejo
curl -s -o /dev/null -w "js -> %{http_code} size=%{size_download}\n" http://127.0.0.1:8080/assets/agent-settings/agent-settings.js
rm -f /tmp/cjs.txt
echo done
