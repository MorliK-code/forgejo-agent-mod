"""e2e #22 — API-мутации для живого прогона мода «Живые обновления».

Песочница hermes/agent-e2e19b (публичная, поллером не опрашивается — тесты
не будят агентов). Запуск с PVE: pct exec 106 -- /opt/agent-loop/venv/bin/python3 \
  /opt/agent-loop/repo/tmp/e2e22_api.py <action> [args]

actions:
  new <title>                 создать задачу, напечатать номер
  comment <num> <text>        добавить комментарий, напечатать id
  delcomment <num> <cid>      удалить комментарий
  label <num> add|rm <name>   метка
  close|reopen <num>          статус
"""
import os
import sys

for _l in open("/opt/agent-loop/env"):
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
sys.path.insert(0, "/opt/agent-loop/repo")
from agent.forgejo_client import ForgejoClient  # noqa: E402

O, R = "hermes", "agent-e2e19b"
c = ForgejoClient("http://127.0.0.1:3000", "hermes", os.environ["FORGEJO_TOKEN"])

act = sys.argv[1]
if act == "new":
    print(c.create_issue(O, R, sys.argv[2], "e2e22 live test task")["number"])
elif act == "comment":
    print(c.create_comment(O, R, int(sys.argv[2]), sys.argv[3])["id"])
elif act == "delcomment":
    c._req("DELETE", f"/repos/{O}/{R}/issues/{sys.argv[2]}/comments/{sys.argv[3]}")
    print("deleted")
elif act == "label":
    if sys.argv[3] == "add":
        c.add_labels(O, R, int(sys.argv[2]), [sys.argv[4]])
    else:
        c.remove_labels(O, R, int(sys.argv[2]), [sys.argv[4]])
    print(sorted(c.issue_labels(O, R, int(sys.argv[2]))))
elif act == "close":
    c._req("PATCH", f"/repos/{O}/{R}/issues/{sys.argv[2]}", json={"state": "closed"})
    print("closed")
elif act == "reopen":
    c._req("PATCH", f"/repos/{O}/{R}/issues/{sys.argv[2]}", json={"state": "open"})
    print("open")
else:
    raise SystemExit("unknown action")
