"""本地配置：~/.htmlshare/config.json，环境变量 HTMLSHARE_SERVER / HTMLSHARE_TOKEN 优先。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("HTMLSHARE_CONFIG") or Path.home() / ".htmlshare" / "config.json")


def _read_file() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load() -> dict:
    cfg = _read_file()
    if os.environ.get("HTMLSHARE_SERVER"):
        cfg["server"] = os.environ["HTMLSHARE_SERVER"]
    if os.environ.get("HTMLSHARE_TOKEN"):
        cfg["token"] = os.environ["HTMLSHARE_TOKEN"]
    return cfg


def save(**values) -> Path:
    cfg = _read_file()
    cfg.update({k: v for k, v in values.items() if v is not None})
    for k, v in values.items():
        if v is None:
            cfg.pop(k, None)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
