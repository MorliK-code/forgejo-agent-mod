"""Одноразовый зонд: каким глаголом Forgejo 16 правит цвет метки (POST -> 405)."""
import os
import sys

for _l in open("/opt/agent-loop/env"):
    if "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
sys.path.insert(0, "/opt/agent-loop/repo")
from agent.config import load_live
from agent.forgejo_client import ForgejoClient

cfg = load_live("/opt/agent-loop/config.yaml")
O, R = "hermes", "agent-e2e19"
fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
lab = {l["name"]: l for l in fj._req("GET", f"/repos/{O}/{R}/labels").json()}["5"]
print("before", lab["id"], lab["color"])
for verb in ("PUT", "PATCH", "POST"):
    try:
        r = fj._req(verb, f"/repos/{O}/{R}/labels/{lab['id']}", json={"color": "d1242f"})
        print(verb, "OK", r.status_code, r.json().get("color"))
        break
    except Exception as e:
        print(verb, "FAIL", str(e)[:120])
after = {l["name"]: l["color"] for l in fj._req("GET", f"/repos/{O}/{R}/labels").json()}
print("after", after.get("5"))
