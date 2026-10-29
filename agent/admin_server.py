"""Бэкенд панели «Агент» и mod-системы Forgejo.

Маршруты:
  /agent-admin/api/*          — API панели (overlay, models, mods, state)
  /agent-admin/bootstrap.js   — bootstrap mod-системы (генерируется на запрос;
                                алиас /agent-admin/agent-ui.js)
  /agent-admin/mods/<m>/<f>   — ассеты модов
  /agent-admin/               — fallback-страница

Настройки: config.overlay.yaml (chmod 600) — agent-loop перечитывает каждый
цикл без рестарта. Модели: models.json. Моды: mods/<name>/mod.json.
Auth: сессия самого Forgejo (cookie проверяется через GET /user/login, кэш 30с).
Панель открывается только из-под залогиненного Forgejo — отдельного пароля нет.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import yaml

from . import mods as mods_mod
from . import bridge as bridge_mod
from . import tasks as tasks_mod

BASE = Path(__file__).resolve().parent          # .../repo/agent
HOST = "0.0.0.0"
PORT = int(os.environ.get("AGENT_ADMIN_PORT", "8091"))
# Forgejo (внутренний адрес для проверки сессии)
FORGEJO_INTERNAL = os.environ.get("FORGEJO_INTERNAL_URL", "http://127.0.0.1:3000")
AUTH_CACHE_TTL = 30  # сек, кэш проверки сессии


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default))


CONFIG_FILE = _p("AGENT_CONFIG", "/opt/agent-loop/config.yaml")
MODELS_FILE = _p("AGENT_MODELS_FILE", "/opt/agent-loop/models.json")
FMT_CIDS_FILE = _p("AGENT_FMT_CIDS_FILE", "/opt/agent-loop/fmt-cids.json")
LOG_FILE = _p("AGENT_LOG", "/var/log/agent-loop/agent.log")
SERVICE = "agent-loop"

NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")


# ---------- config ----------
def load_base() -> dict:
    if CONFIG_FILE.exists():
        try:
            return yaml.safe_load(CONFIG_FILE.read_text()) or {}
        except Exception:
            return {}
    return {}


def _overlay_file() -> Path:
    env = os.environ.get("AGENT_CONFIG_OVERLAY")
    return Path(env) if env else CONFIG_FILE.with_name(CONFIG_FILE.name + ".overlay.yaml")


def load_overlay() -> dict:
    p = _overlay_file()
    if p.exists():
        try:
            return yaml.safe_load(p.read_text()) or {}
        except Exception:
            return {}
    return {}


def save_overlay(overlay: dict):
    p = _overlay_file()
    p.write_text(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_models() -> dict:
    if MODELS_FILE.exists():
        try:
            d = json.loads(MODELS_FILE.read_text())
            d.setdefault("default", None)
            d.setdefault("providers", {})
            return d
        except Exception:
            pass
    return {"default": None, "providers": {}}


def save_models(data: dict):
    MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODELS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try:
        os.chmod(MODELS_FILE, 0o600)
    except OSError:
        pass


def load_fmt_cids() -> dict:
    try:
        return json.loads(FMT_CIDS_FILE.read_text())
    except Exception:
        return {}


def save_fmt_cids(d: dict) -> None:
    try:
        FMT_CIDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FMT_CIDS_FILE.write_text(json.dumps(d))
    except OSError:
        log_admin.exception("save fmt-cids failed")


def fmt_cids_key(repo_full: str, number: int) -> str:
    return f"{str(repo_full).strip().strip('/').lower()}#{int(number)}"


def add_fmt_cid(repo_full: str, number: int, cid: int) -> None:
    """Запомнить id отформатированного комментария (окно правки показывает
    «Переформатировать» только для них). shortcut: список на задачу, максимум 300 id."""
    d = load_fmt_cids()
    lst = d.setdefault(fmt_cids_key(repo_full, number), [])
    if int(cid) not in lst:
        lst.append(int(cid))
    if len(lst) > 300:
        del lst[:-300]
    save_fmt_cids(d)


def effective_config() -> dict:
    """База + оверлей + models.json — то, что реально видит агент."""
    from .config import deep_merge, expand
    merged = deep_merge(load_base(), load_overlay())
    m = load_models()
    providers = {**(merged.get("models") or {}).get("providers", {}), **m["providers"]}
    models = {**(merged.get("models") or {}), "providers": providers,
              "default": m.get("default") or (merged.get("models") or {}).get("default")}
    merged["models"] = models
    return expand(merged)


log_admin = logging.getLogger("admin")


def _fmt_body_worker(repo_full: str, number: int) -> None:
    """Форматирование описания созданной задачи (поток)."""
    try:
        cfg = effective_config()
        fj = tasks_mod.client(cfg)
        o, r = str(repo_full).strip().strip("/").split("/", 1)
        issue = fj._req("GET", f"/repos/{o}/{r}/issues/{int(number)}").json()
        bridge_mod.format_body(cfg, fj, o, r, number, issue.get("body") or "")
    except Exception:
        log_admin.exception("fmt_body worker failed: %s#%s", repo_full, number)


def _body_action_worker(kind: str, repo_full: str, number: int) -> None:
    """restore/reformat описания по запросу из окна правки (поток)."""
    fn = {"restore": bridge_mod.restore_body, "reformat": bridge_mod.reformat_body}[kind]
    try:
        cfg = effective_config()
        fj = tasks_mod.client(cfg)
        o, r = str(repo_full).strip().strip("/").split("/", 1)
        issue = fj._req("GET", f"/repos/{o}/{r}/issues/{int(number)}").json()
        fn(cfg, fj, o, r, number, issue.get("body") or "")
    except Exception:
        log_admin.exception("%s_body worker failed: %s#%s", kind, repo_full, number)


def _fmt_comment_worker(repo_full: str, number: int, cid) -> None:
    """Переформатировать комментарий по запросу панели (поток, очередь не нужна).

    cid есть -> по id; иначе — последний не-агентский комментарий (спим 3с,
    чтобы он успел сохраниться после submit).
    """
    time.sleep(3)
    try:
        cfg = effective_config()
        fj = tasks_mod.client(cfg)
        o, r = str(repo_full).strip().strip("/").split("/", 1)
        cs = fj.issue_comments(o, r, number) or []
        agent_login = str((cfg.get("forgejo") or {}).get("user", "hermes")).lower()
        target = None
        if cid:
            target = next((c for c in cs if c.get("id") == int(cid)), None)
        if target is None:
            cand = [c for c in cs
                    if (c.get("user") or {}).get("login", "").lower() != agent_login]
            target = max(cand, key=lambda c: c.get("id", 0), default=None)
        if target is not None:
            if bridge_mod.format_comment(cfg, fj, o, r, target["id"], target.get("body") or ""):
                add_fmt_cid(repo_full, number, target["id"])
    except Exception:
        log_admin.exception("fmt_comment worker failed: %s#%s", repo_full, number)


# ---------- state ----------
def svc_state() -> dict:
    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip()
        except Exception:
            return ""
    return {
        "service": run(["systemctl", "is-active", SERVICE]) or "unavailable",
        "since": run(["systemctl", "show", SERVICE, "--property=ActiveEnterTimestamp",
                      "--value"]),
    }


def log_tail(n: int = 30) -> list[str]:
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        return [l.rstrip() for l in f.readlines()[-n:]]


def open_issues(cfg: dict) -> int:
    try:
        from .forgejo_client import ForgejoClient
        f = cfg.get("forgejo", {})
        fj = ForgejoClient(f["url"], f["user"], f.get("token", ""), timeout=8)
        return len(fj.issues(f["owner"], f["repo"], state="open"))
    except Exception:
        return -1


def state() -> dict:
    cfg = effective_config()
    s = svc_state()
    tail = log_tail(30)
    last = ""
    for l in reversed(tail):
        if "INFO" in l and "agent-loop started" not in l:
            last = l[:52]
            break
    return {
        **s,
        "base": {
            "forgejo": load_base().get("forgejo", {}),
            "agent": load_base().get("agent", {}),
            "ssh_hosts": load_base().get("ssh_hosts", {}),
            "model_count": len((load_base().get("models") or {}).get("providers", {})),
        },
        "overlay": load_overlay(),
        "effective": {
            "forgejo": cfg.get("forgejo", {}),
            "agent": cfg.get("agent", {}),
            "honcho": {k: v for k, v in (cfg.get("honcho") or {}).items() if k != "api_key"},
            "skills": cfg.get("skills", {}),
            "ssh_hosts": cfg.get("ssh_hosts", {}),
            "default_model": (cfg.get("models") or {}).get("default"),
        },
        "mods": mods_mod.list_mods(),
        "last_activity": last,
        "open_issues": open_issues(cfg),
        "log": tail,
    }


# ---------- model test ----------
def test_provider(p: dict) -> dict:
    try:
        r = requests.post(p["base_url"].rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {p.get('api_key', 'not-needed')}"},
                          json={"model": p["model"],
                                "messages": [{"role": "user", "content": "hi"}],
                                "max_tokens": 1, "temperature": 0},
                          timeout=int(p.get("timeout", 120)))
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
        d = r.json()
        return {"ok": True, "model": d.get("model", p["model"]),
                "usage": d.get("usage", {})}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _do_test(b: dict) -> dict:
    d = load_models()
    if b.get("provider"):
        name = b.get("name", "")
        old = d["providers"].get(name, {})
        p = {
            "base_url": (b["provider"].get("base_url") or "").rstrip("/"),
            "model": b["provider"].get("model"),
            "api_key": b["provider"].get("api_key") or old.get("api_key") or "not-needed",
            "timeout": b["provider"].get("timeout") or 120,
        }
    else:
        p = d["providers"].get(b.get("name", ""))
    if not p:
        return {"ok": False, "error": "модель не найдена"}
    try:
        from .config import expand
        p = expand(p)
    except Exception:
        pass
    return test_provider(p)


def _do_test_mod(mod_name: str, sub: dict) -> dict:
    """Тест из мода: sub = {models: {name: true}} → прогнать тесты моделей."""
    if "models" in sub:
        results = {}
        for mn in sub["models"]:
            results[mn] = _do_test({"name": mn})
        return {"ok": all(r.get("ok") for r in results.values() if r),
                "results": results}
    return {"ok": False, "error": "неизвестный тест"}


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Агент</title></head>
<body style="font-family:sans-serif;padding:40px;color:#333">
<h3>Панель «Агент»</h3>
<p>Открывается только из-под залогиненного Forgejo: войдите в Forgejo
и зайдите сюда по ссылке из настроек (user/settings#agent).</p>
</body></html>"""

# ---------- auth: сессия Forgejo ----------
_auth_cache: dict[str, tuple[float, bool]] = {}


def _forgejo_session_ok(cookie_header: str) -> bool:
    """Проверяем сессию: GET /user/login с браузерным cookie.
    Forgejo: залогиненный редиректит (3xx), гостю отдаёт страницу логина (200).
    API в этом Forgejo отключён, поэтому /api/noone недоступен.
    Кэш на AUTH_CACHE_TTL по cookie — не бьём каждый запрос."""
    key = hashlib.sha256(cookie_header.encode()).hexdigest()
    now = time.time()
    hit = _auth_cache.get(key)
    if hit and now - hit[0] < AUTH_CACHE_TTL:
        return hit[1]
    def probe(hdr: str) -> bool:
        try:
            r = requests.get(FORGEJO_INTERNAL + "/user/login",
                             headers={"Cookie": hdr}, timeout=5,
                             allow_redirects=False)
            return 300 <= r.status_code < 400
        except Exception:
            return False

    parts = [c.strip() for c in cookie_header.split(";") if c.strip()]
    sid_only = "; ".join(c for c in parts if not c.startswith("persistent="))
    per_only = "; ".join(c for c in parts if c.startswith("persistent="))
    # #21: persistent не форвардим, когда есть sid — автологин ротит sid и осиротивает браузер (405).
    # #24: но у сессии «запомнить меня» sid может отсутствовать: проверяем по persistent,
    #      иначе у залогиненного пользователя панели модов молча исчезают (401).
    ok = (probe(sid_only) if sid_only else False) or (probe(per_only) if per_only else False)
    _auth_cache[key] = (now, ok)
    if len(_auth_cache) > 256:
        for k, _ in sorted(_auth_cache.items(), key=lambda kv: kv[1][0])[:64]:
            _auth_cache.pop(k, None)
    return ok


# ---------- handler ----------
def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _session_ok(self) -> bool:
            return _forgejo_session_ok(self.headers.get("Cookie", ""))

        def _send(self, code: int, body: bytes,
                  ctype: str = "application/json; charset=utf-8",
                  nocache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if nocache:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            # bootstrap + ассеты модов: нужны БЕЗ кока (страница Forgejo их грузит)
            if path in ("/agent-admin/bootstrap.js", "/agent-admin/agent-ui.js"):
                self._send(200, mods_mod.bootstrap_js().encode(),
                           "application/javascript; charset=utf-8", True)
                return
            if path.startswith("/agent-admin/mods/"):
                rest = path[len("/agent-admin/mods/"):]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    data = mods_mod.mod_asset(parts[0], parts[1])
                    if data is not None:
                        ctype = ("application/javascript; charset=utf-8"
                                 if parts[1].endswith(".js")
                                 else "text/css; charset=utf-8"
                                 if parts[1].endswith(".css")
                                 else "application/octet-stream")
                        self._send(200, data, ctype, True)
                        return
                self._send(404, b"not found")
                return

            if not self._session_ok():
                if path in ("/agent-admin", "/"):
                    self._send(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8", True)
                else:
                    self._send(401, b"")
                return
            if path == "/agent-admin/api/state":
                self._json(state())
            elif path == "/agent-admin/api/models":
                self._json(load_models())
            elif path == "/agent-admin/api/tasks/config":
                self._json(tasks_mod.config_view(effective_config()))
            elif path == "/agent-admin/api/tasks/fmt_cids":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                repo = num = ""
                for kv in q.split("&"):
                    if kv.startswith("repo="):
                        repo = kv[5:]
                    elif kv.startswith("number="):
                        num = kv[7:]
                try:
                    import urllib.parse as _u
                    self._json({"cids": load_fmt_cids().get(
                        fmt_cids_key(_u.unquote(repo), int(num or 0)), [])})
                except Exception as e:
                    self._json({"error": str(e)[:200]}, 502)
            elif path == "/agent-admin/api/tasks/labels":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                repo = num = ""
                for kv in q.split("&"):
                    if kv.startswith("repo="):
                        repo = kv[5:]
                    elif kv.startswith("number="):
                        num = kv[7:]
                try:
                    import urllib.parse as _u
                    self._json(tasks_mod.issue_labels_view(
                        effective_config(), _u.unquote(repo), int(num or 0)))
                except Exception as e:
                    self._json({"error": str(e)[:200]}, 502)
            elif path == "/agent-admin/api/tasks":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                st = "open"
                for kv in q.split("&"):
                    if kv.startswith("state=") and kv[6:] in ("open", "closed", "all"):
                        st = kv[6:]
                try:
                    self._json(tasks_mod.list_tasks(effective_config(), st))
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path in ("/agent-admin", "/"):
                self._send(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8", True)
            else:
                self._json({"error": "not found"}, 404)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            b = self._read_body()
            if not self._session_ok():
                self._send(401, b"")
                return
            if path == "/agent-admin/api/overlay":
                self._save_overlay(b)
            elif path == "/agent-admin/api/models":
                self._save_model(b)
            elif path == "/agent-admin/api/models/test":
                self._json(_do_test(b))
            elif path == "/agent-admin/api/models/default":
                d = load_models()
                if b.get("name") in d["providers"]:
                    d["default"] = b["name"]
                    save_models(d)
                    self._json({"ok": True})
                else:
                    self._json({"error": "нет такой модели"}, 404)
            elif path == "/agent-admin/api/models/delete":
                d = load_models()
                n = b.get("name", "")
                if n in d["providers"]:
                    del d["providers"][n]
                    if d.get("default") == n:
                        d["default"] = next(iter(d["providers"]), None)
                    save_models(d)
                    self._json({"ok": True})
                else:
                    self._json({"error": "нет такой модели"}, 404)
            elif path == "/agent-admin/api/mods":
                self._json(mods_mod.list_mods())
            elif path == "/agent-admin/api/mods/enable":
                self._json({"ok": mods_mod.set_enabled(
                    b.get("name", ""), b.get("enabled", True))})
            elif path == "/agent-admin/api/mods/deploy":
                # deploy = копирование templates/public в $FORGEJO_CUSTOM + рестарт forgejo
                self._json(mods_mod.deploy_mod(b.get("name", "")))
            elif path == "/agent-admin/api/mods/undeploy":
                self._json(mods_mod.undeploy_mod(b.get("name", "")))
            elif path == "/agent-admin/api/mods/delete":
                self._json(mods_mod.delete_mod(b.get("name", "")))
            elif path == "/agent-admin/api/tasks":
                try:
                    self._json(tasks_mod.create_task(
                        effective_config(), b.get("title", ""), b.get("body", ""),
                        b.get("priority", ""), bool(b.get("agent")), b.get("due", "")))
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path in ("/agent-admin/api/tasks/close", "/agent-admin/api/tasks/reopen"):
                try:
                    self._json(tasks_mod.set_state(
                        effective_config(), int(b.get("number") or 0),
                        path.endswith("close")))
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path == "/agent-admin/api/tasks/tag":
                try:
                    cfg = effective_config()
                    res = tasks_mod.tag_task(
                        cfg, b.get("repo", ""), int(b.get("number") or 0),
                        b.get("priority"),
                        (bool(b["agent"]) if "agent" in b else None),
                        b.get("due", ""),
                        (bool(b["format"]) if "format" in b else None),
                        (bool(b["pause"]) if "pause" in b else None))
                    if b.get("pause") is True:   # метка = действие сразу
                        bridge_mod.stop_run(cfg, f"{res['repo']}#{res['number']}")
                    self._json(res)
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path == "/agent-admin/api/tasks/fmt_comment":
                try:
                    threading.Thread(
                        target=_fmt_comment_worker,
                        args=(b.get("repo", ""), int(b.get("number") or 0), b.get("cid")),
                        daemon=True).start()
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path == "/agent-admin/api/tasks/fmt_body":
                try:
                    threading.Thread(
                        target=_fmt_body_worker,
                        args=(b.get("repo", ""), int(b.get("number") or 0)),
                        daemon=True).start()
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path in ("/agent-admin/api/tasks/restore_body",
                          "/agent-admin/api/tasks/reformat_body"):
                kind = "restore" if path.endswith("restore_body") else "reformat"
                try:
                    threading.Thread(
                        target=_body_action_worker,
                        args=(kind, b.get("repo", ""), int(b.get("number") or 0)),
                        daemon=True).start()
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path == "/agent-admin/api/tasks/attach":
                try:
                    self._json(tasks_mod.attach_repo(
                        effective_config(), int(b.get("number") or 0), b.get("repo", "")))
                except Exception as e:
                    self._json({"error": str(e)[:300]}, 502)
            elif path.startswith("/agent-admin/api/mods/test/"):
                name = path.rsplit("/", 1)[-1]
                self._json(_do_test_mod(name, b))
            else:
                self._json({"error": "not found"}, 404)

        def _save_overlay(self, b: dict):
            ov = load_overlay()
            pause_on = None
            if "forgejo" in b:
                f = dict(ov.get("forgejo", {}))
                if "poll_interval" in b["forgejo"]:
                    v = b["forgejo"]["poll_interval"]
                    if not (isinstance(v, int) and not isinstance(v, bool) and 5 <= v <= 3600):
                        self._json({"error": "poll_interval: целое 5..3600"}, 400)
                        return
                    f["poll_interval"] = v
                if "trigger_labels" in b["forgejo"]:
                    v = b["forgejo"]["trigger_labels"]
                    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                        self._json({"error": "trigger_labels: список строк"}, 400)
                        return
                    f["trigger_labels"] = v
                if "checkbox" in b["forgejo"]:
                    f["checkbox"] = str(b["forgejo"]["checkbox"])[:30]
                if "paused" in b["forgejo"]:
                    pause_on = bool(b["forgejo"]["paused"])
                    f["paused"] = pause_on
                ov["forgejo"] = f
            if "agent" in b:
                a = dict(ov.get("agent", {}))
                if "max_iterations" in b["agent"]:
                    v = b["agent"]["max_iterations"]
                    if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 500):
                        self._json({"error": "max_iterations: целое 1..500"}, 400)
                        return
                    a["max_iterations"] = v
                ov["agent"] = a
            if "ssh_hosts" in b:
                v = b["ssh_hosts"]
                if not isinstance(v, dict):
                    self._json({"error": "ssh_hosts: объект name -> ip"}, 400)
                    return
                for k, ip in v.items():
                    if (not re.match(r"^[a-z0-9_-]{1,20}$", k)
                            or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(ip))):
                        self._json({"error": f"ssh_hosts: '{k}: {ip}' некорректно"}, 400)
                        return
                ov["ssh_hosts"] = {k: str(ip) for k, ip in v.items()}
            if "model_default" in b and b["model_default"] is not None:
                d = load_models()
                if b["model_default"] not in d["providers"]:
                    self._json({"error": "model_default: нет такой модели"}, 400)
                    return
                d["default"] = b["model_default"]
                save_models(d)
            save_overlay(ov)
            if pause_on is None:
                self._json({"ok": True})
                return
            try:
                cfg = effective_config()
                if pause_on:      # стоп живого прогона — первое дело, метки догоняют
                    try:
                        bridge_mod.stop_run(cfg, str(json.loads(bridge_mod.RUN_FILE.read_text())["key"]))
                    except Exception:
                        pass
                res = tasks_mod.set_global_pause(cfg, pause_on)
            except Exception as e:
                self._json({"error": f"pause: {str(e)[:200]}"}, 502)
                return
            self._json({"ok": True, **res})

        def _save_model(self, b: dict):
            name = (b.get("name") or "").strip()
            old_name = (b.get("old_name") or "").strip()
            p = b.get("provider") or {}
            if not NAME_RE.fullmatch(name):
                self._json({"error": "имя: буквы/цифры/_/-, до 40"}, 400)
                return
            if not p.get("base_url") or not p.get("model"):
                self._json({"error": "base_url и model обязательны"}, 400)
                return
            d = load_models()
            if old_name and old_name != name and name in d["providers"]:
                self._json({"error": "модель с таким именем уже есть"}, 400)
                return
            old = d["providers"].get(old_name or name, {})
            provider = {
                "base_url": p["base_url"].rstrip("/"),
                "model": p["model"],
                "api_key": p.get("api_key") or old.get("api_key") or "not-needed",
                "max_tokens": int(p.get("max_tokens") or 4096),
                "temperature": float(p.get("temperature", 0.3)),
                "timeout": int(p.get("timeout") or 600),
            }
            d["providers"][name] = provider
            # rename: старое имя удаляем, default переносим на новое
            if old_name and old_name != name:
                d["providers"].pop(old_name, None)
                if d.get("default") == old_name:
                    d["default"] = name
            if b.get("set_default"):
                d["default"] = name
            elif d.get("default") is None and len(d["providers"]) == 1:
                d["default"] = name
            save_models(d)
            self._json({"ok": True})

    return H


def _pause_monitor():
    """#25: метка 'pause' (или глобальная пауза) + чат-гейт останавливают ЖИВОЙ прогон.

    Поллер блокируется на ssh-прогоне и метку увидит только после его окончания,
    поэтому стоп делает наблюдатель: run.json = что сейчас в работе. Плюс при
    активной гейт-паузе — свип сирот (прогонов вне run.json) раз в ~10с.
    shortcut: свип периодический; при параллельных прогонах перейти на событие."""
    log = logging.getLogger("agent-admin")
    stopped = ""
    sweep_tick = 0
    while True:
        time.sleep(2)  # метка/пауза должны реагировать быстро
        try:
            cfg = effective_config()
            paused = bool(cfg["forgejo"].get("paused"))
            try:
                run = json.loads(bridge_mod.RUN_FILE.read_text())
            except Exception:
                run = None
            gate = paused
            if run:
                key = str(run.get("key") or "")
                try:
                    names = [l.get("name") if isinstance(l, dict) else l
                             for l in (tasks_mod.issue_labels_view(cfg, f"{run['owner']}/{run['repo']}",
                                                                   int(run["num"])).get("labels") or [])]
                    gate = paused or "pause" in names or bridge_mod.chat_gate_active(cfg)
                    if key and key != stopped:
                        if gate:
                            bridge_mod.stop_run(cfg, key)
                            stopped = key
                            log.info("pause/chat: прогон %s остановлен (%s)", key,
                                     "глобальная пауза" if paused
                                     else ("чат-гейт" if gate else "метка pause"))
                        else:
                            # #28 watchdog: сессия живого прогона молчит выше порога —
                            # висящий headless держит flock и жрёт GPU до run_timeout
                            wd = bridge_mod.watchdog_idle(cfg, str(run.get("session") or ""),
                                                          str(run.get("started") or ""))
                            if wd is not None:
                                bridge_mod.mark_watchdog_stop(key)  # учет wake без fails++
                                bridge_mod.stop_run(cfg, key)
                                stopped = key
                                log.info("watchdog: прогон %s остановлен (тишина %ds)",
                                         key, int(wd))
                except Exception as e:
                    log.warning("pause monitor: %s", e)
            else:
                stopped = ""
            sweep_tick += 1
            if gate and not run and sweep_tick >= 5:
                sweep_tick = 0
                if bridge_mod.sweep_agent_runs(cfg):
                    log.info("sweep: сироты-прогоны ублены (пауза/чат-гейт)")
        except Exception as e:
            log.warning("pause monitor: %s", e)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=_pause_monitor, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), make_handler())
    print(f"agent-admin on {HOST}:{PORT} (auth=forgejo-session {FORGEJO_INTERNAL})",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
