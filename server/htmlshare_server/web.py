"""服务端自己的几个小页面（首页、无权限、CLI 授权确认等）。"""

from __future__ import annotations

from datetime import datetime
from html import escape

from fastapi.responses import HTMLResponse

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2329;--muted:#646a73;--line:#e5e6eb;--accent:#3370ff;--warn:#d83931}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1e1f22;--fg:#e6e7ea;--muted:#9aa0a8;--line:#2e3035;--accent:#5b8cff;--warn:#f26b63}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
.muted{color:var(--muted)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}.wrap{overflow-x:auto}
code,pre{font-family:ui-monospace,Consolas,monospace;font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 18px;font-size:15px;cursor:pointer}
.code{font-size:28px;letter-spacing:4px;font-weight:600}.warn{color:var(--warn)}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 6px;margin:0 4px 4px 0;font-size:12px}
"""


def fmt_ts(ts: int | None, never: str = "永不过期") -> str:
    if ts is None:
        return never
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def layout(title: str, body: str, status: int = 200, user_name: str | None = None) -> HTMLResponse:
    who = ""
    if user_name is not None:
        who = f'<span class="muted">{escape(user_name)} · <a href="/auth/logout">退出</a></span>'
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>{CSS}</style></head><body><main>
<header><h1><a href="/" style="color:inherit">HTML 分享</a></h1>{who}</header>
{body}</main></body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def message(title: str, text: str, status: int = 200, user_name: str | None = None) -> HTMLResponse:
    return layout(title, f'<div class="card"><h2>{escape(title)}</h2><p>{text}</p></div>', status, user_name)


def home(user_name: str, public_url: str, pages: list[dict], install_cmd: str) -> HTMLResponse:
    if pages:
        rows = []
        for p in pages:
            tags = "".join(f'<span class="tag">{escape(v["label"])}</span>' for v in p["viewers"]) or '<span class="muted">仅自己</span>'
            rows.append(
                f'<tr><td><a href="/{escape(p["name"])}.html" target="_blank">{escape(p["name"])}</a>'
                f'<div class="muted">{escape(p["title"])}</div></td><td>{tags}</td>'
                f'<td>{p["views"]}</td><td>{escape(fmt_ts(p["updated_ts"]))}</td><td>{escape(fmt_ts(p["expires_ts"]))}</td></tr>'
            )
        table = (
            '<div class="wrap"><table><tr><th>页面</th><th>可见人</th><th>访问次数</th><th>更新时间</th><th>过期时间</th></tr>'
            + "".join(rows)
            + "</table></div>"
        )
    else:
        table = '<p class="muted">还没有分享过页面。</p>'
    body = f"""
<div class="card"><h2>我分享的页面</h2>{table}</div>
<div class="card"><h2>在 AI 工具里使用</h2>
<p>1. 安装客户端并登录（会打开浏览器走飞书登录）：</p>
<pre>{escape(install_cmd)}
htmlshare login --server {escape(public_url)}</pre>
<p>2. 命令行发布：</p>
<pre>htmlshare publish report.html --name q3-report --to zhangsan@example.com,lisi@example.com</pre>
<p>3. 或者把它接成 MCP（Claude Code 示例）：</p>
<pre>claude mcp add htmlshare -- htmlshare mcp</pre>
<p class="muted">没法打开浏览器的环境（比如远程服务器），可以在这里生成一个 API Token，设成环境变量 HTMLSHARE_TOKEN 和 HTMLSHARE_SERVER。</p>
<form method="post" action="/me/tokens"><button type="submit">生成 API Token</button></form>
</div>"""
    return layout("HTML 分享", body, user_name=user_name)


def forbidden(user_name: str, owner_name: str, page: str) -> HTMLResponse:
    who = escape(owner_name) if owner_name else "分享者"
    return message(
        "没有访问权限",
        f"你（{escape(user_name)}）不在页面 <code>{escape(page)}</code> 的可见名单里，请联系 <b>{who}</b> 把你加进去。",
        status=403,
        user_name=user_name,
    )


def dev_login(state: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>开发模式登录</h2>
<p class="warn">当前是 HTMLSHARE_DEV_FAKE_LOGIN 开发模式，不经过飞书。生产环境必须关闭。</p>
<form method="get" action="/auth/callback">
<input type="hidden" name="state" value="{escape(state)}">
<p>邮箱：<input name="email" required placeholder="zhangsan@example.com"></p>
<p>姓名：<input name="name" placeholder="张三"></p>
<button type="submit">登录</button></form></div>"""
    return layout("开发模式登录", body)


def cli_verify(user_name: str, user_code: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>授权命令行登录</h2>
<p>有一个 htmlshare 客户端正在请求以你（{escape(user_name)}）的身份登录。请确认终端里显示的代码与下面一致：</p>
<p class="code">{escape(user_code)}</p>
<p class="muted">如果不是你本人刚刚运行的 <code>htmlshare login</code>，请直接关闭此页面。</p>
<form method="post" action="/cli/verify"><input type="hidden" name="user_code" value="{escape(user_code)}">
<button type="submit">确认授权</button></form></div>"""
    return layout("授权命令行登录", body, user_name=user_name)


def token_created(user_name: str, token: str, public_url: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>API Token 已生成</h2>
<p class="warn">只显示这一次，请立即保存。它等同于你的身份，不要发给别人。</p>
<pre>HTMLSHARE_SERVER={escape(public_url)}
HTMLSHARE_TOKEN={escape(token)}</pre>
<p><a href="/">返回首页</a></p></div>"""
    return layout("API Token", body, user_name=user_name)
