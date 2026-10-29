#!/usr/bin/env python3
"""e2e32_dbg — v3: structure of ?template= form page."""
import re
import requests

B = "http://192.168.110.9:8080"
s = requests.Session()
s.headers["Sec-Fetch-Site"] = "same-origin"
s.post(B + "/user/login", data={"user_name": "e2e32_probe", "password": "e2e32probe2026"},
       allow_redirects=False)
r = s.get(B + "/Hermes/agent-e2e32/issues/new?template=.forgejo%2fISSUE_TEMPLATE%2ftask.yaml")
t = r.text
print("FINAL:", r.url, r.status_code, "len", len(t))
print("forms:", re.findall(r"<form[^>]*>", t))
print("names:", re.findall(r'name="([^"]+)"', t))
# embedded page data (JSON blob)
for m in re.findall(r'<script[^>]*>.*?</script>', t, re.S):
    if "template" in m or "criteria" in m or "Критерии" in m:
        print("SCRIPT-BLOB:", re.sub(r"\s+", " ", m)[:500])
        break
for m in re.findall(r'data-[a-z-]+="[^"]{40,300}"', t):
    print("DATA-ATTR:", m[:320])
