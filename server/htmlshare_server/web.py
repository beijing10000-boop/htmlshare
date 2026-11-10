"""控制台页面的渲染：Jinja2 模板（templates/）+ 静态资源（static/）。

外观沿用运维控制台共用的设计系统 ui-kit：static/ui-kit.css、ui-kit.js 与 AI Ops Platform 仓库里的逐字节相同
（tests/test_ui_kit.py 按 ui-kit.sha256 核对），不要在这里改；本站自己的样式和脚本写在 app.css、app.js。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import __version__

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"

# 控制台页面没有内联脚本和内联样式，策略可以收紧到只认本站资源；用户发布的页面走另一套（sandbox，见 config.py）
CONSOLE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CONSOLE_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}

# ui-kit 图标集里没有的几个图标（同样的 24 格、1.7 线宽）；ui-kit 自带的用 data-icon 属性
ICONS = {
    "logo": '<path d="M7 3.5h7l4.5 4.5v11a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5Z"/><path d="M14 3.5V8h4.5"/><path d="m10.5 12.5-2 2 2 2m3-4 2 2-2 2"/>',
    "page": '<path d="M7 3.5h7l4.5 4.5v11a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5Z"/><path d="M14 3.5V8h4.5M9 12.5h6M9 16h4"/>',
    "inbox": '<path d="M4 13.5 6.2 5.6a1.5 1.5 0 0 1 1.4-1.1h8.8a1.5 1.5 0 0 1 1.4 1.1L20 13.5v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4Z"/><path d="M4 13.5h4.5a3.5 3.5 0 0 0 7 0H20"/>',
    "request": '<circle cx="10" cy="8" r="3.5"/><path d="M3.5 19.5a6.5 6.5 0 0 1 13 0M18.5 8v6m-3-3h6"/>',
    "globe": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.6 2.6 14.4 0 17m0-17c-2.6 2.6-2.6 14.4 0 17"/>',
    "upload": '<path d="M12 15.5v-11m-4 4 4-4 4 4M4.5 15.5v4h15v-4"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6A1.5 1.5 0 0 0 14 4.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    "open": '<path d="M13.5 4.5h6v6m0-6L11 13M10 6.5H6A1.5 1.5 0 0 0 4.5 8v10A1.5 1.5 0 0 0 6 19.5h10a1.5 1.5 0 0 0 1.5-1.5v-4"/>',
}


def icon(name: str) -> Markup:
    return Markup(
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>'
    )


def fmt_ts(ts: int | None, never: str = "—") -> str:
    return never if ts is None else datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_ago(ts: int | None, never: str = "—") -> str:
    """刚刚 / 5 分钟前 / 3 小时前 / 6 天前；超过 30 天写日期。"""
    if ts is None:
        return never
    gap = max(0, int(time.time()) - ts)
    if gap < 60:
        return "刚刚"
    if gap < 3600:
        return f"{gap // 60} 分钟前"
    if gap < 86400:
        return f"{gap // 3600} 小时前"
    if gap < 30 * 86400:
        return f"{gap // 86400} 天前"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def fmt_left(ts: int | None) -> str:
    """离过期还有多久：剩 3 天 / 剩 5 小时 / 即将过期。"""
    if ts is None:
        return "永不过期"
    gap = ts - int(time.time())
    if gap >= 86400:
        return f"剩 {gap // 86400} 天"
    if gap >= 3600:
        return f"剩 {gap // 3600} 小时"
    return "即将过期"


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, round(n / 1024))} KB" if n else "0 KB"


env = Environment(loader=FileSystemLoader(BASE / "templates"), autoescape=True, trim_blocks=True, lstrip_blocks=True)
env.filters.update(ts=fmt_ts, ago=fmt_ago, left=fmt_left, size=fmt_size)
env.globals.update(icon=icon, version=__version__)


@dataclass
class Shell:
    """侧栏和顶栏要用的：当前用户、是不是管理员、待处理申请数、当前在哪个导航项。"""

    user_name: str
    is_admin: bool
    pending: int
    active: str = ""

    @property
    def initial(self) -> str:
        return (self.user_name.strip()[:1] or "?").upper()


def render(template: str, status: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(template).render(**ctx), status_code=status, headers=HEADERS)


def message(title: str, text: str, status: int = 200, user_name: str | None = None, link: tuple[str, str] | None = None) -> HTMLResponse:
    """独立的提示页（登录出错、页面不存在、授权成功……）。text 是调用方拼好的 HTML，里面的变量调用方自己转义。"""
    return render("message.html", status, title=title, text=Markup(text), user_name=user_name, link=link)
