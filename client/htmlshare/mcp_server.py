"""MCP server（stdio）：把分享服务的能力暴露成工具给 AI 用。"""

from __future__ import annotations

from pathlib import Path

from typing import Callable, TypeVar

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

from .api import Client, HtmlShareError

T = TypeVar("T")

INSTRUCTIONS = """把 HTML 页面发布到公司内网分享站。访问者要先用飞书登录，并且在可见名单里才能打开。
- 生成 HTML 报告或页面后，如果用户想分享给同事、或者想要一个链接，就用 publish_html 发布，然后把返回的 url 发给用户。
- 可见人用飞书邮箱或手机号；all 表示全员可见。用户没说给谁看时，先问清楚，不要擅自设成 all。
- 返回结果里 unresolved 不为空，说明有人没加上，要告诉用户原因。"""

server = _Server("htmlshare", instructions=INSTRUCTIONS)


def make_client() -> Client:
    return Client()


def _call(op: Callable[[Client], T]) -> T:
    # 只有 ToolError 的消息会原样传给模型，其他异常 SDK 只回 "Error executing tool"
    try:
        return op(make_client())
    except HtmlShareError as e:
        raise ToolError(str(e)) from None


def _brief(page: dict) -> dict:
    out = {
        "url": page["url"],
        "name": page["name"],
        "title": page.get("title", ""),
        "viewers": [v["label"] for v in page.get("viewers", [])],
        "expires_at": page.get("expires_at"),
    }
    for key in ("created", "unresolved", "views"):
        if key in page:
            out[key] = page[key]
    return out


@server.tool()
def publish_html(
    file_path: str = "",
    html_content: str = "",
    name: str = "",
    viewers: list[str] | None = None,
    expires_days: int | None = None,
) -> dict:
    """把 HTML 发布到内网分享站，返回访问链接。

    参数：
    - file_path：本地 HTML 文件的绝对路径（优先使用，省 token）。
    - html_content：没有文件时直接传 HTML 文本。file_path 和 html_content 二选一。
    - name：页面名，会出现在链接里（https://分享站/<name>.html），只能用小写字母、数字、-。不填则取文件名。
      同一个人用同名再次发布 = 覆盖更新；名字被别人占用会报错，换个名字即可。
    - viewers：可见人列表，填飞书邮箱或手机号，["all"] 表示全员。
      不填时：新页面只有自己可见，已有页面保留原来的可见人。发布者本人总是可见。
    - expires_days：多少天后过期，0 表示永不过期；不填用服务端默认（通常 30 天）。
    """
    if file_path and not Path(file_path).expanduser().is_absolute():
        raise ToolError(f"file_path 需要绝对路径（MCP server 的工作目录和你的不同）：{file_path}")
    page = _call(
        lambda c: c.publish(
            file_path or None,
            content=html_content or None,
            name=name or None,
            viewers=viewers,
            expires_days=expires_days,
        )
    )
    return _brief(page)


@server.tool()
def update_viewers(
    name: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    set_to: list[str] | None = None,
    expires_days: int | None = None,
) -> dict:
    """修改已发布页面的可见人或过期时间。

    - add：追加可见人（飞书邮箱 / 手机号 / all）。
    - remove：移除可见人（按添加时填的邮箱 / 手机号匹配；all 表示取消全员可见）。
    - set_to：整体替换可见人；传 [] 表示只有自己可见。
    - expires_days：从现在起多少天后过期，0 表示永不过期。
    """
    page = _call(lambda c: c.update(name, set_viewers=set_to, add_viewers=add, remove_viewers=remove, expires_days=expires_days))
    return _brief(page)


@server.tool()
def list_my_pages() -> list[dict]:
    """列出我发布过的页面（链接、可见人、访问次数、过期时间）。"""
    return [_brief(p) for p in _call(lambda c: c.list_pages())]


@server.tool()
def delete_page(name: str) -> dict:
    """删除我发布的一个页面，删除后链接立即失效。"""
    return _call(lambda c: c.delete(name))


@server.tool()
def whoami() -> dict:
    """查看当前登录的飞书身份和分享站地址。未登录时会报错，提示用户在终端运行 htmlshare login。"""
    return _call(lambda c: c.me())


def run() -> None:
    server.run("stdio")
