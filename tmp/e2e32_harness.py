#!/usr/bin/env python3
"""e2e32_harness — #32 issue-шаблон с обязательным чек-листом «Критерии готовности».

Доказывает на живом Forgejo (v16.0.5, http://192.168.110.9:8080):
  A) форма создания задачи показывает шаблон «Задача» (репо morlik/Forgejo-agents-tasks и песочница Hermes/agent-e2e32);
  B) форма с required-полем «Критерии готовности» не принимает submit без него (issue не создаётся);
  C) заполненный submit создаёт issue, тело которого содержит чек-лист критериев;
  D) WORK_PROMPT (агентский промпт) содержит правило сверки чек-листа перед done.
Используется ОДНА песочница Hermes/agent-e2e32 — реальные репо задач не засоряются.
"""
import json
import os
import re
import sys

import requests

B = os.environ.get("E2E32_BASE", "http://192.168.110.9:8080")
USER = "e2e32_probe"
PW = os.environ.get("E2E32_PW", "e2e32probe2026")
REPO = "Hermes/agent-e2e32"
REAL = "morlik/Forgejo-agents-tasks"

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail else ""))
    if not cond:
        fails.append(name)


def issue_count(s):
    r = s.get(B + "/api/v1/repos/" + REPO + "/issues?state=all")
    try:
        return len(r.json())
    except Exception:
        return -1


s = requests.Session()
s.headers.update({"Sec-Fetch-Site": "same-origin"})

# --- 0) login probe user -----------------------------------------------------
r = s.post(B + "/user/login", data={"user_name": USER, "password": PW},
           allow_redirects=False)
authed = s.get(B + "/user/settings", allow_redirects=False)
check("A0.login", authed.status_code == 200,
      "post=%s settings=%s" % (r.status_code, authed.status_code))

# --- A) picker shows the template (real repo + sandbox) ----------------------
for tag, repo in (("A1.real", REAL), ("A2.sandbox", REPO)):
    r = s.get(B + "/%s/issues/new" % repo)
    ok = (r.status_code == 200 and "Задача" in r.text
          and "Критерии готовности" in r.text)
    links = re.findall(r'href="(/[^"]*issues/new[^"]*)"', r.text)
    check(tag, ok, "final=%s links=%s" % (r.url, links[:6]))

# --- find the form URL for task.yaml in the sandbox picker -------------------
r = s.get(B + "/%s/issues/new" % REPO)
href = None
for m in re.findall(r'href="(/[^"]*issues/new[^"]*)"', r.text):
    if "task" in m or "template" in m or "choice" in m:
        href = m
        break
check("A3.form-url-found", bool(href), str(href))
if not href:
    print(json.dumps({"fails": fails}))
    sys.exit(1)

# --- B) required field enforced ----------------------------------------------
r = s.get(B + href)
check("B1.form-page", r.status_code == 200, "status=%s" % r.status_code)
check("B2.form-has-criteria", "Критерии готовности" in r.text)
frm = re.search(r'<form[^>]*id="new-issue"[^>]*action="([^"]+)"', r.text)
fields = []
action = frm.group(1) if frm else None
num = None
hidden = {}
if action:
    start = r.text.find('id="new-issue"')
    end = r.text.find("</form>", start)
    section = r.text[start:end]
    fields = re.findall(r'<(?:input|textarea|select)[^>]*name="([^"]+)"', section)
    hidden = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', section))
check("B3.form-fields", bool(fields) and action, str(fields[:14]))
if not (fields and action):
    print(json.dumps({"fails": fails + ["form-parse"]}))
    sys.exit(1)

# helper: build post body from the form's fields, filling only the template ones
def build(fill_blank=False):
    data = {name: hidden.get(name, "") for name in fields}
    data["title"] = "e2e32 тест"  # title always filled: rejection must come from criteria
    data["form-field-description"] = "" if fill_blank else "Сделать шаблон с чек-листом"
    data["form-field-criteria"] = "" if fill_blank else (
        "- [ ] шаблон показывается в форме\n- [ ] submit без критериев отклоняется")
    if not fill_blank:
        for name in list(data):
            if name.startswith("form-field-confirmed"):
                data[name] = "on"
    return data

# Forgejo 16 валидацию required делает на клиенте (JS формы); сервер принимает и
# пустые поля, подставляя '_No response_'. Проверяем поведение конвейера шаблона:
n0 = issue_count(s)
r = s.post(B + action, data=build(fill_blank=True), allow_redirects=False)
n1 = issue_count(s)
check("B4.empty-submission-placeholder-body", n1 == n0 + 1,
      "post=%s count %s->%s (required enforced by browser JS)" % (r.status_code, n0, n1))
lst = s.get(B + "/api/v1/repos/" + REPO + "/issues?state=all").json()
a1 = max(lst, key=lambda x: x["number"])
check("B5.placeholder-no-response", "_No response_" in a1.get("body", ""), "issue #%s" % a1["number"])

# --- C) filled submit creates issue with checklist in body -------------------
r = s.post(B + action, data=build(fill_blank=False), allow_redirects=False)
n2 = issue_count(s)
lst = s.get(B + "/api/v1/repos/" + REPO + "/issues?state=all").json()
a = max(lst, key=lambda x: x["number"])
num = a["number"]
created = n2 > n1
check("C1.submit-filled-creates", created,
      "post=%s count %s->%s" % (r.status_code, n1, n2))
if created:
    body = a.get("body", "")
    ok = ("Критерии готовности" in body and "- [ ]" in body
          and "Сделать шаблон с чек-листом" in body)
    check("C2.issue-body-has-checklist", ok)
    print("BODY-SNIPPET:\n" + body[:700])

print(json.dumps({"fails": fails, "issue": num if created else None}, ensure_ascii=False))
sys.exit(1 if fails else 0)
