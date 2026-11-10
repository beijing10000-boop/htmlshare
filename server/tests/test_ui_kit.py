"""ui-kit 是运维控制台共用的设计系统，这里的两份文件必须和上游逐字节相同（换行按 LF 算）。

升级方法：从 AI Ops Platform 仓库把 ui-kit.css、ui-kit.js、ui-kit.sha256 三个文件一起拷过来；
本站自己的样式写在 app.css，不要改 ui-kit。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "htmlshare_server" / "static"


def manifest() -> dict[str, str]:
    lines = (STATIC / "ui-kit.sha256").read_text("utf-8").splitlines()
    return {name: digest for digest, name in (line.split() for line in lines if line and not line.startswith("#"))}


def test_ui_kit_files_match_the_manifest():
    listed = manifest()
    assert set(listed) == {"ui-kit.css", "ui-kit.js"}
    for name, digest in listed.items():
        data = (STATIC / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"{name} 被改过了：页面自己的样式请写到 app.css"


def test_ui_kit_version_is_the_same_everywhere():
    versions = {
        re.search(r"ui-kit (\d+\.\d+\.\d+)", (STATIC / name).read_text("utf-8")).group(1)
        for name in ("ui-kit.css", "ui-kit.js", "ui-kit.sha256")
    }
    assert len(versions) == 1, versions
