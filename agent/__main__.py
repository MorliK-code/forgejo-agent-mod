"""python -m agent --config /opt/agent-loop/config.yaml [--once]"""
import argparse
import sys

from .config import load
from .forgejo_client import ForgejoClient
from .honcho_client import Honcho
from .poller import _load_state, main, poll_once, setup_logging


def _build(cfg):
    fj = ForgejoClient(cfg["forgejo"]["url"], cfg["forgejo"]["user"], cfg["forgejo"]["token"])
    hc = cfg.get("honcho")
    hon = (Honcho(hc["base_url"], hc["api_key"], hc["workspace"], hc["peer"], hc["ai_peer"])
           if hc and hc.get("api_key") else None)
    return fj, hon


def entry(cfg_path: str, once: bool = False):
    cfg = load(cfg_path)
    log_path = cfg.get_path("agent", "log", default="agent.log")
    setup_logging(log_path)
    fj, hon = _build(cfg)
    if once:
        n = poll_once(cfg, fj, hon, _load_state(cfg_path))
        print(f"one cycle done, launched={n}")
    else:
        main(cfg_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/agent-loop/config.yaml")
    ap.add_argument("--once", action="store_true", help="один цикл поллинга и выход")
    a = ap.parse_args()
    entry(a.config, a.once)
    sys.exit(0)
