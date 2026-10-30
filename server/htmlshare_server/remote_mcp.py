"""远程 MCP（Streamable HTTP，挂在 /mcp）：AI 工具直接连分享站，本机不用装任何东西。

认证用和 CLI 一样的 API token（Authorization: Bearer hs_...）。服务端读不到用户本机的文件，
所以发布本地文件分两步：prepare_upload 拿一次性上传地址 → AI 在终端用 curl 把文件 PUT 上来；
不能执行命令的工具才用 publish_html 直接传 HTML 内容。
"""

import json
from urllib.parse import urlsplit

import anyio
from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import Settings
from .db import Database
from .service import PageService, auto_name, hash_secret

INSTRUCTIONS = """把 HTML 页面发布到公司内网分享站。访问者要先用飞书登录，并且在可见名单里才能打开。

发布方式（二选一）：
1. 本地有 HTML 文件、而且你能执行终端命令（推荐，不占对话 token）：先调用 prepare_upload 拿到一次性上传地址，
   再在终端执行它返回的 curl 命令上传文件（Windows PowerShell 里要写 curl.exe）。命令输出的 JSON 里 url 就是访问链接。
2. 不能执行命令时：调用 publish_html，把完整 HTML 放在 html_content 里（文件大时会很慢）。
发布成功后把访问链接发给用户。

可见人（viewers）可以写：姓名（"张三"）、飞书邮箱、手机号、"dept:部门名"（含子部门）、"all"（全员）。
用户没说给谁看时先问清楚，不要擅自设成 all。发布者本人总是可见。

返回结果里 unresolved 不为空，说明有人或部门没加上，要告诉用户原因：
- 重名的人：提示里列出了每个人的部门和邮箱；能从上下文判断是谁就改用那个邮箱，判断不了就问用户。
- 同名部门：按提示里的完整路径重新加（例如 dept:技术中心/运维部），不确定就问用户。
不确定某人是谁、在哪个部门时，先用 find_user 查。"""


def brief(page: dict) -> dict:
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


def bearer_owner(db: Database, authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return db.token_owner(hash_secret(authorization[7:].strip()))


class BearerGate:
    """挡在 MCP 前面：没有有效 token 直接 401，不进 MCP 会话。"""

    def __init__(self, app, db: Database):
        self.app = app
        self.db = db

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), None)
            if not bearer_owner(self.db, auth):
                body = json.dumps(
                    {
                        "error": "unauthorized",
                        "detail": "缺少或无效的 token：请在分享站首页点「接入 AI 工具」生成，并配置请求头 Authorization: Bearer <token>",
                    },
                    ensure_ascii=False,
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def connect_prompt(public_url: str, token: str) -> str:
    """给用户复制给 AI 工具的那段话：让 AI 自己把远程 MCP 配好。"""
    url = f"{public_url}/mcp"
    cursor = json.dumps({"url": url, "headers": {"Authorization": f"Bearer {token}"}}, ensure_ascii=False)
    return f"""请帮我把公司内网的 HTML 分享工具 htmlshare 接入你自己。它是远程 MCP 服务（Streamable HTTP 传输），不需要安装任何软件：
- 地址：{url}
- 认证：请求头 Authorization: Bearer {token}

按你自己的类型配置（配置写到用户级，不要写进项目目录或提交到 git——这个 token 等同于我的身份）：
- 如果你是 Claude Code：运行 claude mcp add --transport http --scope user htmlshare {url} --header "Authorization: Bearer {token}"，再用 claude mcp get htmlshare 确认
- 如果你是 Cursor：在 ~/.cursor/mcp.json 的 mcpServers 里加入 "htmlshare": {cursor}
- 其他支持远程 MCP 的工具：按它自己的方式配置上面的地址和请求头

配置完成后告诉我是否需要重启工具或新开会话；生效后调用 htmlshare 的 whoami 工具，确认返回的是我的名字。"""


def build_remote_mcp(service: PageService, db: Database, s: Settings) -> tuple[BearerGate, MCPServer]:
    server = MCPServer("htmlshare", instructions=INSTRUCTIONS, version=__version__)

    def owner_of(ctx: Context) -> str:
        owner = bearer_owner(db, (ctx.headers or {}).get("authorization"))
        if not owner:
            raise ToolError("token 无效或已撤销，请在分享站首页重新「接入 AI 工具」")
        return owner

    async def run(fn, *args, **kwargs):
        # 业务逻辑里有 SQLite 和飞书接口调用，放到线程里，别卡住事件循环
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except HTTPException as e:
            raise ToolError(str(e.detail)) from None

    @server.tool()
    async def prepare_upload(ctx: Context, name: str = "", viewers: list[str] | None = None, expires_days: int | None = None) -> dict:
        """发布本地 HTML 文件的第一步：拿一个一次性上传地址（30 分钟有效、只能用一次）。

        拿到后在终端执行返回的 how_to_upload 里的命令，把 <HTML文件路径> 换成文件的实际路径。
        上传命令输出的 JSON 里 url 就是访问链接。

        - name：页面名，会出现在链接里（/<name>.html），只能用小写字母、数字、-；通常用文件名的英文部分。不填自动生成。
          同一个人用同名再次发布 = 覆盖更新；名字被别人占用会报错，换个名字即可。
        - viewers：可见人列表，每项可以是姓名、飞书邮箱、手机号、"dept:部门名"（含子部门）或 "all"（全员）。
          不填时：新页面只有自己可见，已有页面保留原来的可见人。查不到/重名的会列在 unresolved 里。
        - expires_days：多少天后过期，0 表示永不过期；不填用默认（30 天）。
        """
        return await run(service.prepare_upload, owner_of(ctx), name, viewers, expires_days)

    @server.tool()
    async def publish_html(
        ctx: Context, html_content: str, name: str = "", viewers: list[str] | None = None, expires_days: int | None = None
    ) -> dict:
        """直接把 HTML 内容发布到分享站，返回访问链接。只在不能执行终端命令时用；能执行命令请用 prepare_upload + curl。

        参数含义同 prepare_upload；html_content 是完整的 HTML 文本。
        """
        page_name = name if name.strip() else auto_name()
        page = await run(service.publish, owner_of(ctx), page_name, html_content.encode("utf-8"), viewers, expires_days)
        return brief(page)

    @server.tool()
    async def update_viewers(
        ctx: Context,
        name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        set_to: list[str] | None = None,
        expires_days: int | None = None,
    ) -> dict:
        """修改已发布页面的可见人或过期时间。

        - add：追加可见人（姓名 / 飞书邮箱 / 手机号 / dept:部门名 / all）。
        - remove：移除可见人（姓名 / 邮箱 / 手机号 / dept:部门名或路径；all 表示取消全员可见）。
        - set_to：整体替换可见人；传 [] 表示只有自己可见。
        - expires_days：从现在起多少天后过期，0 表示永不过期。
        """
        page = await run(service.update, owner_of(ctx), name, set_to, add, remove, expires_days)
        return brief(page)

    @server.tool()
    async def list_my_pages(ctx: Context) -> dict:
        """列出我发布过的页面（链接、可见人、访问次数、过期时间）。"""
        pages = await run(service.list_pages, owner_of(ctx))
        return {"pages": [brief(p) for p in pages]}

    @server.tool()
    async def delete_page(ctx: Context, name: str) -> dict:
        """删除我发布的一个页面，删除后链接立即失效。"""
        return await run(service.delete, owner_of(ctx), name)

    @server.tool()
    async def find_user(ctx: Context, query: str) -> dict:
        """按姓名、英文名或邮箱的一部分查同事，返回姓名、部门、邮箱和 open_id（最多 20 个，完全匹配的排前面）。

        用在：用户只说了名字但有重名、或者不确定是谁时，先查出邮箱再用邮箱作为可见人。
        """
        owner_of(ctx)
        return await run(service.search_users, query)

    @server.tool()
    async def whoami(ctx: Context) -> dict:
        """查看当前连接对应的飞书身份和分享站地址。"""
        return await run(service.me, owner_of(ctx))

    netloc = urlsplit(s.public_url).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[netloc, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[s.origin, "http://127.0.0.1:*", "http://localhost:*"],
    )
    inner = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # 每个请求独立处理：服务重启不丢会话，多人并发也简单
        json_response=True,
        max_request_body_size=service.max_bytes * 2 + (1 << 20),  # publish_html 的 HTML 经过 JSON 转义会变大
        transport_security=security,
    )
    return BearerGate(inner, db), server
