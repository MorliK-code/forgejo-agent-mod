"""Forgejo mod-фреймворк.

Мод = каталог MODS_DIR/<name>/:
  mod.json    — {name, title, enabled, description}
  templates/  — go-шаблоны, НАТИВНО оверраят Gitea: deploy копирует
                в $FORGEJO_CUSTOM/templates/<relpath> и рестартует forgejo
                (Gitea перечитывает custom/ при старте)
  public/     — статика: deploy копирует в $FORGEJO_CUSTOM/public/<relpath>,
                раздаётся Gitea под /assets/<relpath>
  (опц.) js/css-инжекты (pages + assets в mod.json) — без деплоя, для
                лёгких модов, которым не нужен серверный рендер

Состояние деплоя: mods/<name>/.deploy.json — {files: {target_rel: sha256}}.
Undeploy удаляет только файлы, идентичные версии мода (hash) — чужие
ручные файлы и oверраи других модов не трогаются.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MODS_DIR = Path(os.environ.get("AGENT_MODS_DIR",
                               str(Path(__file__).resolve().parent.parent / "mods")))
FORGEJO_CUSTOM = Path(os.environ.get("FORGEJO_CUSTOM_DIR",
                                     "/home/git/forgejo/custom"))

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_FORGEJO_SERVICE = os.environ.get("FORGEJO_SERVICE", "forgejo")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_file(name: str) -> Path:
    return MODS_DIR / name / ".deploy.json"


def list_mods() -> list[dict]:
    out = []
    if not MODS_DIR.exists():
        return out
    for d in sorted(MODS_DIR.iterdir()):
        if not d.is_dir():
            continue
        mf = d / "mod.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
        except Exception:
            m = {}
        m.setdefault("enabled", False)
        m.setdefault("pages", [])
        m.setdefault("assets", {})
        m["dir"] = d.name
        m["deployed"] = _manifest_file(d.name).exists()
        m["has_templates"] = (d / "templates").is_dir()
        m["has_public"] = (d / "public").is_dir()
        # regex'ы injection-части — валидируем
        pages = []
        for p in m["pages"]:
            try:
                re.compile(p)
                pages.append(p)
            except re.error:
                pass
        m["pages"] = pages
        out.append(m)
    return out


def get_mod(name: str) -> dict | None:
    if not _SAFE_NAME.fullmatch(name):
        return None
    mf = MODS_DIR / name / "mod.json"
    if not mf.exists():
        return None
    try:
        m = json.loads(mf.read_text())
    except Exception:
        return None
    m["dir"] = name
    m["deployed"] = _manifest_file(name).exists()
    return m


def set_enabled(name: str, enabled: bool) -> bool:
    """Пишет только канон-поля — runtime-ключи (dir/deployed/has_*) не
    сохраняются, иначе mod.json грязнится и git не тянется."""
    mf = MODS_DIR / name / "mod.json"
    if not mf.exists():
        return False
    try:
        m = json.loads(mf.read_text())
    except Exception:
        return False
    m["enabled"] = bool(enabled)
    mf.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return True


def mod_asset(name: str, path: str) -> bytes | None:
    """Чтение ассета мода (injection-часть, без выхода из каталога)."""
    if not _SAFE_NAME.fullmatch(name):
        return None
    if not path or ".." in path or path.startswith("/"):
        return None
    f = (MODS_DIR / name / path).resolve()
    base = (MODS_DIR / name).resolve()
    if not str(f).startswith(str(base)) or not f.is_file():
        return None
    return f.read_bytes()


# ---------- deploy / undeploy (нативные oверраи) ----------
def _restart_forgejo() -> bool:
    try:
        r = subprocess.run(["systemctl", "restart", _FORGEJO_SERVICE],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _scan_mod_files(name: str) -> dict[str, tuple[Path, str]]:
    """{target_rel: (source_path, sub)} для каталогов templates/ и public/.

    public/<rel> мапится в public/assets/<rel>, т.к. Gitea раздаёт кастомную
    статику из $CUSTOM/public/assets/* под URL /assets/* (см. modules/public
    + docs: $GITEA_CUSTOM/public/assets/impressum.html → /assets/impressum.html).
    """
    mod_dir = MODS_DIR / name
    out: dict[str, tuple[Path, str]] = {}
    for sub in ("templates", "public"):
        root = mod_dir / sub
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                rel = f.relative_to(root).as_posix()
                target_rel = f"{sub}/assets/{rel}" if sub == "public" else f"{sub}/{rel}"
                out[target_rel] = (f, sub)
    return out


def deploy_mod(name: str) -> dict:
    m = get_mod(name)
    if not m:
        return {"ok": False, "error": "мод не найден"}
    files = _scan_mod_files(name)
    if not files:
        return {"ok": False, "error": "у мода нет каталогов templates/ или public/"}
    if not FORGEJO_CUSTOM.exists():
        return {"ok": False, "error": f"нет {FORGEJO_CUSTOM}"}

    deployed: dict[str, str] = {}
    our_man = deploy_state(name) or {}
    for target_rel, (src, sub) in sorted(files.items()):
        dst = FORGEJO_CUSTOM / sub / target_rel[len(sub) + 1:]
        data = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        # не затираем файл, который не наш (ручной кастом Gitea или чужой мод)
        if dst.exists():
            existing = dst.read_bytes()
            existing_h = _sha256(existing)
            ours = our_man.get("files", {}).get(target_rel) == existing_h
            if existing_h != _sha256(data) and not ours and not _is_ours(target_rel, name, existing):
                return {"ok": False,
                        "error": f"конфликт: {target_rel} занят чужим файлом"}
        dst.write_bytes(data)
        deployed[target_rel] = _sha256(data)

    _manifest_file(name).write_text(json.dumps({
        "mod": name,
        "files": deployed,
        "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=1))

    ok = _restart_forgejo()
    return {"ok": ok, "deployed": len(deployed),
            **({} if ok else {"error": "forgejo не перестартовал — проверь systemctl"})}


def _is_ours(target_rel: str, except_mod: str, data: bytes) -> bool:
    """Файл на диске принадлежит какому-то задеплоенному моду (кроме except_mod)?"""
    if not MODS_DIR.exists():
        return False
    h = _sha256(data)
    for mf in MODS_DIR.glob("*/.deploy.json"):
        mod = mf.parent.name
        if mod == except_mod:
            continue
        try:
            man = json.loads(mf.read_text())
        except Exception:
            continue
        if man.get("files", {}).get(target_rel) == h:
            return True
    return False


def undeploy_mod(name: str) -> dict:
    m = get_mod(name)
    if not m:
        return {"ok": False, "error": "мод не найден"}
    if m.get("core"):
        return {"ok": False,
                "error": "core-мод нельзя выключать (это сам мод-менеджер)"}
    man_f = _manifest_file(name)
    if not man_f.exists():
        return {"ok": False, "error": "мод не задеплоен"}
    try:
        man = json.loads(man_f.read_text())
    except Exception:
        return {"ok": False, "error": "битый .deploy.json — удали вручную"}

    removed, skipped = 0, 0
    for target_rel, h in man.get("files", {}).items():
        sub = target_rel.split("/", 1)[0]
        dst = FORGEJO_CUSTOM / sub / target_rel[len(sub) + 1:]
        if dst.exists():
            try:
                if _sha256(dst.read_bytes()) == h:
                    dst.unlink()
                    removed += 1
                    # убираем пустые каталоги до уровня sub
                    p = dst.parent
                    for _ in range(target_rel.count("/")):
                        if p.exists() and not any(p.iterdir()):
                            p.rmdir()
                            p = p.parent
                        else:
                            break
                else:
                    skipped += 1  # файл изменили/заменили — не трогаем
            except OSError:
                skipped += 1
    man_f.unlink()
    _restart_forgejo()
    return {"ok": True, "removed": removed, "skipped": skipped}


def deploy_state(name: str) -> dict | None:
    f = _manifest_file(name)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


# ---------- удаление мода (каталог из репо + из custom/) ----------
def _git_toplevel() -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(MODS_DIR), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return None


def _git(repo: Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # systemd не даёт HOME — без неё credential-store не найдёт ~/.git-credentials
    env.setdefault("HOME", "/root")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def delete_mod(name: str) -> dict:
    m = get_mod(name)
    if not m:
        return {"ok": False, "error": "мод не найден"}
    if m.get("core"):
        return {"ok": False, "error": "core-мод нельзя удалять (это сам мод-менеджер)"}

    # 1) если задеплоен — undeploy (файлы из custom/ + рестарт)
    removed = 0
    if m.get("deployed"):
        u = undeploy_mod(name)
        removed = u.get("removed", 0)

    # 2) каталог из репо: git rm + commit + push
    d = MODS_DIR / name
    repo = _git_toplevel()
    pushed, push_err = True, None
    if repo:
        # git rm -f: каталог может быть «грязный» (runtime-правки) — всё равно убираем
        r = _git(repo, ["rm", "-r", "-f", "-q", f"mods/{name}"])
        if r.returncode != 0:
            return {"ok": False,
                    "error": "git rm не прошёл: " + (r.stderr or r.stdout).strip()[:200]}
        # identity для коммита (на CT user.* не задан)
        if not _git(repo, ["config", "user.email"]).stdout.strip():
            _git(repo, ["config", "user.name", "agent-mods"])
            _git(repo, ["config", "user.email", "agent-mods@local"])
        c = _git(repo, ["commit", "-q", "-m", f"delete mod {name}", "--", f"mods/{name}"])
        out = (c.stdout + c.stderr).lower()
        if c.returncode != 0 and "nothing to commit" not in out:
            return {"ok": False,
                    "error": "git commit не прошёл: " + (c.stderr or c.stdout).strip()[:200]}
        p = _git(repo, ["push", "-q", "origin", "HEAD"])
        if p.returncode != 0:
            pushed, push_err = False, (p.stderr or p.stdout).strip()[:200]
    else:
        # вне git (локальная разработка) — просто удаляем каталог
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # 3) добираем файлы, которые git не тронул (untracked/.deploy.json)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

    return {"ok": True, "removed": removed, "pushed": pushed, "push_error": push_err}


# ---------- bootstrap (injection-моды: js/css без рестарта) ----------
BOOTSTRAP_TPL = r"""(function(){
'use strict';
var BASE=__BASE__;
var MODS=__MODS__;
var seen={};
function add(dir,f,kind){
  var url=BASE+'/mods/'+dir+'/'+f;
  if(seen[url]) return; seen[url]=1;
  if(kind==='css'){
    var l=document.createElement('link');
    l.rel='stylesheet';l.href=url;
    document.head.appendChild(l);
  }else{
    var s=document.createElement('script');
    s.src=url;s.defer=true;
    document.head.appendChild(s);
  }
}
function load(){
  var path=location.pathname;
  for(var i=0;i<MODS.length;i++){
    var m=MODS[i];
    var hit=false;
    for(var j=0;j<m.pages.length;j++){
      try{ if(new RegExp(m.pages[j]).test(path)){ hit=true; break; } }catch(e){}
    }
    if(!hit) continue;
    (m.assets.css||[]).forEach(function(f){ add(m.dir,f,'css'); });
    (m.assets.js||[]).forEach(function(f){ add(m.dir,f,'js'); });
  }
}
if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',load);
}else{load();}
})();
"""


def bootstrap_js(base_url: str = "/__mods__") -> str:
    """Bootstrap только для injection-модов (js/css без templates/public).
    Моды с templates/ или public/ деплоятся нативно и в bootstrap не попадают.
    Каждый ассет получает ?v=<md5-хеш содержимого> — при любом изменении файла
    браузер тянет свежий, кэш не мешает деплою."""
    import hashlib
    mods = []
    for m in list_mods():
        if not (m["enabled"] and (m["pages"] or m["assets"])
                and not (m.get("has_templates") or m.get("has_public"))):
            continue
        assets = {}
        for kind in ("js", "css"):
            files = []
            for f in (m["assets"].get(kind) or []):
                try:
                    data = MODS_DIR / m["dir"] / f
                    if data.exists():
                        h = hashlib.md5(data.read_bytes()).hexdigest()[:8]
                        files.append(f"{f}?v={h}")
                        continue
                except Exception:
                    pass
                files.append(f)
            if files:
                assets[kind] = files
        mods.append({"dir": m["dir"], "pages": m["pages"], "assets": assets})
    return BOOTSTRAP_TPL.replace("__BASE__", json.dumps(base_url)) \
                        .replace("__MODS__", json.dumps(mods))
