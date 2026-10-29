"""Загрузка конфига: YAML + подстановка ${ENV_VAR}."""
import os
import re
from pathlib import Path

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(obj):
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    if isinstance(obj, str):
        def repl(m):
            return os.environ.get(m.group(1), "")
        return _ENV_RE.sub(repl, obj)
    return obj


def expand(obj):
    """Публичный вариант _expand — подстановка ${ENV_VAR} в произвольных данных."""
    return _expand(obj)


def deep_merge(base: dict, overlay: dict) -> dict:
    """overlay поверх base: вложенные dict сливаются, скаляры/списки — заменяются.
    Значение null в overlay стирает ключ (возврат к базе)."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _overlay_path(path: Path) -> Path:
    env = os.environ.get("AGENT_CONFIG_OVERLAY")
    return Path(env) if env else path.with_name(path.name + ".overlay.yaml")


def load_live(path: str | Path) -> "Config":
    """База + оверлей (config.overlay.yaml рядом с базой). Оверлей перечитывается
    на каждом вызове — панель управления пишет туда без рестарта агента."""
    path = Path(path)
    cfg = load(path)
    op = _overlay_path(path)
    if op.exists():
        try:
            raw = op.read_text(encoding="utf-8")
            ovl = _expand(yaml.safe_load(raw) or {})
            base = {k: v for k, v in cfg.items() if k != "__path__"}
            merged = deep_merge(base, ovl)
            merged["__path__"] = str(path.resolve())
            cfg = Config(merged)
        except Exception:
            pass  # битый оверлей не должен валить агента
    return cfg


class Config(dict):
    def get_path(self, *keys, default=None):
        cur = self
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    data = _expand(yaml.safe_load(raw))
    cfg = Config(data)
    cfg["__path__"] = str(path.resolve())
    # секреты не должны попадать в логи
    return cfg
