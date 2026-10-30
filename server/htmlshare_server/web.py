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
button.secondary{background:transparent;color:var(--accent);border:1px solid var(--line);padding:4px 12px;font-size:14px}
form.inline{display:inline;margin:0}
textarea{width:100%;font-family:ui-monospace,Consolas,monospace;font-size:13px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:12px;resize:vertical}
details summary{cursor:pointer}
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


def shared_table(shared: list[dict], dept_error: str | None) -> str:
    note = f'<p class="warn">{escape(dept_error)}</p>' if dept_error else ""
    if not shared:
        return note + '<p class="muted">还没有人分享页面给你。</p>'
    rows = "".join(
        f'<tr><td><a href="/{escape(p["name"])}.html" target="_blank">{escape(p["title"] or p["name"])}</a>'
        f'<div class="muted">{escape(p["name"])}</div></td><td>{escape(p["owner_name"] or "—")}</td>'
        f'<td><span class="tag">{escape(p["via"])}</span></td>'
        f'<td>{escape(fmt_ts(p["updated_ts"]))}</td><td>{escape(fmt_ts(p["expires_ts"]))}</td></tr>'
        for p in shared
    )
    return (
        note
        + '<div class="wrap"><table><tr><th>页面</th><th>发布者</th><th>你能看到是因为</th><th>更新时间</th><th>过期时间</th></tr>'
        + rows
        + "</table></div>"
    )


def home(
    user_name: str,
    public_url: str,
    pages: list[dict],
    install_cmd: str,
    tokens: list[dict],
    shared: list[dict],
    dept_error: str | None = None,
) -> HTMLResponse:
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
        table = '<p class="muted">还没有发布过页面。</p>'
    if tokens:
        token_rows = "".join(
            f'<tr><td>{escape(t["label"] or "（未命名）")}</td><td>{escape(fmt_ts(t["created_at"]))}</td>'
            f'<td>{escape(fmt_ts(t["last_used_at"], never="从未使用"))}</td>'
            f'<td><form method="post" action="/me/tokens/{int(t["id"])}/revoke" class="inline">'
            f'<button type="submit" class="secondary">撤销</button></form></td></tr>'
            for t in tokens
        )
        token_table = (
            '<div class="wrap"><table><tr><th>名称</th><th>创建时间</th><th>最近使用</th><th></th></tr>' + token_rows + "</table></div>"
        )
    else:
        token_table = '<p class="muted">还没有。</p>'
    body = f"""
<div class="card"><h2>接入 AI 工具</h2>
<p>不用安装任何软件：点下面的按钮，把生成的一段话复制发给你的 AI 工具（Claude Code、Cursor 等），它会自己完成配置。</p>
<form method="post" action="/me/connect"><button type="submit">接入 AI 工具</button></form>
<p class="muted">接好以后直接对 AI 说「把这个报告发布出去，给张三和技术中心看」就行。
可见人可以说姓名、飞书邮箱、手机号、部门（含子部门）或全员。</p>
</div>
<div class="card"><h2>我发布的页面</h2>{table}</div>
<div class="card"><h2>分享给我的页面</h2>{shared_table(shared, dept_error)}</div>
<div class="card" id="tokens"><h2>我的接入凭证</h2>
<p class="muted">每次「接入 AI 工具」或命令行登录都会生成一个凭证。凭证等同于你的身份；不用的、或者可能泄露的，在这里撤销。</p>
{token_table}</div>
<div class="card"><details><summary><b>命令行客户端（可选）</b>：Claude Desktop 等不支持远程 MCP 的工具用</summary>
<p>需要 Python 3.10+：</p>
<pre>{escape(install_cmd)}
htmlshare login --server {escape(public_url)}
claude mcp add --scope user htmlshare -- htmlshare mcp</pre>
<p class="muted">没法打开浏览器的环境（比如远程服务器），可以生成一个凭证，设成环境变量 HTMLSHARE_TOKEN 和 HTMLSHARE_SERVER。</p>
<form method="post" action="/me/tokens"><button type="submit" class="secondary">生成命令行凭证</button></form>
</details></div>"""
    return layout("HTML 分享", body, user_name=user_name)


def connect(user_name: str, prompt: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>接入 AI 工具</h2>
<p>把下面这段话<b>完整复制</b>，发给你的 AI 工具（Claude Code、Cursor 等），它会自己完成配置并确认接通。</p>
<textarea id="prompt" readonly rows="16">{escape(prompt)}</textarea>
<p><button id="copy" type="button">复制这段话</button> <span id="copied" class="muted"></span></p>
<p class="warn">这段话里含有你的接入凭证，只显示这一次。它等同于你的身份：不要发到群里或发给别人。
不用了可以在首页「我的接入凭证」里撤销。</p>
<p class="muted">Claude Desktop 等不支持远程 MCP 的工具，请用首页「命令行客户端」的方式。</p>
<p><a href="/">返回首页</a></p></div>
<script>
document.getElementById("copy").onclick = function () {{
  var t = document.getElementById("prompt"), tip = document.getElementById("copied"), ok = false;
  t.focus(); t.select();
  try {{ ok = document.execCommand("copy"); }} catch (e) {{}}
  if (!ok && navigator.clipboard) {{ navigator.clipboard.writeText(t.value).then(function () {{ tip.textContent = "已复制"; }}); return; }}
  tip.textContent = ok ? "已复制" : "复制失败，请手动全选复制";
}};
</script>"""
    return layout("接入 AI 工具", body, user_name=user_name)


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
