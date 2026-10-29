#!/usr/bin/env bash
# Деплой agent-loop в CT с Forgejo (CT106). Запускать с PVE: bash deploy.sh
# (или скопировать репо в CT и выполнить там).
set -euo pipefail
CT=106
DEST=/opt/agent-loop

# 1) код в CT
pct exec "$CT" -- mkdir -p "$DEST"
# (репо должно быть склонировано в CT; git-креды CT — отдельный токен)
pct exec "$CT" -- bash -lc "cd $DEST && git pull --ff-only 2>/dev/null || true"

# 2) venv + deps
pct exec "$CT" -- bash -lc "
  python3 -m venv $DEST/venv
  $DEST/venv/bin/pip install -q -r $DEST/agent/requirements.txt
"

# 3) конфиг + env (первый раз — из шаблона)
pct exec "$CT" -- bash -lc "
  [ -f $DEST/config.yaml ] || cp $DEST/agent/config.yaml $DEST/config.yaml
  [ -f $DEST/env ] || cp $DEST/deploy/env.example $DEST/env
  chmod 600 $DEST/env
"

# 4) ssh-ключ CT -> хосты хомелаба (для инструмента ssh_exec)
pct exec "$CT" -- bash -lc "[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519"
KEY=$(pct exec "$CT" -- cat /root/.ssh/id_ed25519.pub)
for h in 192.168.110.2 192.168.110.3 192.168.110.4 192.168.110.5 192.168.110.6 192.168.110.7; do
  # pubkey добавить через ssh-copy-id (пароль root — из доверенного канала)
  echo "add to $h: $KEY"
done

# 5) systemd
pct exec "$CT" -- bash -lc "cp $DEST/deploy/agent-loop.service /etc/systemd/system/ && systemctl daemon-reload"
pct exec "$CT" -- bash -lc "systemctl enable agent-loop && systemctl restart agent-loop"
pct exec "$CT" -- systemctl is-active agent-loop
echo "deploy done. Log: pct exec $CT -- journalctl -u agent-loop -f"
