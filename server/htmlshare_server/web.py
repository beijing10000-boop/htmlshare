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
h3{font-size:15px;margin:20px 0 8px}
.ok{color:#2e7d32}
.notice{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0;background:var(--bg)}
.notice .cands{margin-top:6px}.notice .cands form{margin:4px 6px 0 0}
.tag.removable{padding:2px 4px 2px 8px;font-size:13px}
button.x{background:transparent;color:var(--muted);border:0;padding:0 4px;font-size:14px;line-height:1}
button.x:hover{color:var(--warn)}
button.danger{background:var(--warn)}
form.add-viewer{display:flex;gap:8px;margin-top:10px}
form.add-viewer input[name=viewers]{flex:1;min-width:0;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font-size:14px}
select{padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
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
                f'<td>{p["views"]}</td><td>{escape(fmt_ts(p["updated_ts"]))}</td><td>{escape(fmt_ts(p["expires_ts"]))}</td>'
                f'<td><a href="/me/pages/{escape(p["name"])}">设置</a></td></tr>'
            )
        table = (
            '<div class="wrap"><table><tr><th>页面</th><th>可见人</th><th>访问次数</th><th>更新时间</th><th>过期时间</th><th></th></tr>'
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
<p>不用安装任何软件：点下面的按钮，把生成的一段话复制发给你的 AI 工具（Claude Code、Cursor、Codex 等），它会自己完成配置。</p>
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


def _fmt_iso(value: str | None) -> str:
    return value[:16].replace("T", " ") if value else "永不过期"


def _action_form(name: str, action: str, label: str, extra: str = "", cls: str = "secondary", confirm: str = "") -> str:
    onsubmit = f' onsubmit="return confirm(\'{escape(confirm)}\')"' if confirm else ""
    return (
        f'<form method="post" action="/me/pages/{escape(name)}" class="inline"{onsubmit}>'
        f'<input type="hidden" name="action" value="{escape(action)}">{extra}'
        f'<button type="submit" class="{cls}">{escape(label)}</button></form>'
    )


def page_settings(
    user_name: str,
    page: dict,
    expiry_choices: list[int],
    notice: str | None = None,
    unresolved: list[dict] | None = None,
    error: str | None = None,
) -> HTMLResponse:
    name = page["name"]
    viewers = page["viewers"]
    if viewers:
        tags = "".join(
            f'<span class="tag removable">{escape(v["label"])}'
            + _action_form(name, "remove", "×", f'<input type="hidden" name="value" value="{escape(v["value"])}">', cls="x")
            + "</span>"
            for v in viewers
        )
    else:
        tags = '<span class="muted">仅自己可见</span>'
    msgs = ""
    if error:
        msgs += f'<p class="warn">{escape(error)}</p>'
    if notice:
        msgs += f'<p class="ok">{escape(notice)}</p>'
    for u in unresolved or []:
        cands = "".join(
            _action_form(name, "add", f"加上 {c['label']}", f'<input type="hidden" name="viewers" value="{escape(c["value"])}">')
            for c in u.get("candidates", [])
        )
        msgs += f'<div class="notice"><b>{escape(u["input"])}</b>：{escape(u["reason"])}<div class="cands">{cands}</div></div>'
    options = "".join(f'<option value="{d}">{d} 天后</option>' for d in expiry_choices) + '<option value="0">永不过期</option>'
    has_all = any(v["kind"] == "all" for v in viewers)
    quick = ""
    if not has_all:
        quick += _action_form(name, "add", "设为全员可见", '<input type="hidden" name="viewers" value="all">')
    if viewers:
        quick += " " + _action_form(name, "set_private", "改为仅自己可见", confirm="确定移除全部可见人？之后只有你自己能打开。")
    body = f"""<div class="card"><h2>页面设置</h2>
<p><b>{escape(page["title"] or name)}</b> <span class="muted">{escape(name)}</span></p>
<p><a href="{escape(page["url"])}" target="_blank">{escape(page["url"])}</a></p>
{msgs}
<h3>谁能看</h3>
<div class="viewers">{tags}</div>
<form method="post" action="/me/pages/{escape(name)}" class="add-viewer">
<input type="hidden" name="action" value="add">
<input name="viewers" id="viewer-input" list="viewer-suggest" autocomplete="off" required
 placeholder="输入姓名、飞书邮箱、手机号或部门，多个用逗号分隔">
<datalist id="viewer-suggest"></datalist>
<button type="submit">添加</button></form>
<p class="muted">发布者本人总是可见。部门包含其所有子部门。输入时会联想同事和部门。</p>
<p>{quick}</p>
<h3>过期时间</h3>
<p>当前：{escape(_fmt_iso(page["expires_at"]))}</p>
<form method="post" action="/me/pages/{escape(name)}" class="inline">
<input type="hidden" name="action" value="expiry">
<select name="days">{options}</select> <button type="submit" class="secondary">修改</button></form>
<h3>删除</h3>
<p class="muted">删除后链接立即失效，不能恢复。</p>
<p>{_action_form(name, "delete", "删除这个页面", cls="danger", confirm="确定删除这个页面？链接会立即失效，不能恢复。")}</p>
<p><a href="/">返回首页</a></p></div>
<script>
(function () {{
  var input = document.getElementById("viewer-input"), list = document.getElementById("viewer-suggest"), timer;
  input.addEventListener("input", function () {{
    clearTimeout(timer);
    var q = input.value.split(/[,，]/).pop().trim();
    if (!q) {{ list.innerHTML = ""; return; }}
    timer = setTimeout(function () {{
      fetch("/me/suggest?q=" + encodeURIComponent(q)).then(function (r) {{ return r.json(); }}).then(function (d) {{
        list.innerHTML = "";
        (d.items || []).forEach(function (it) {{
          var o = document.createElement("option"); o.value = it.value; o.label = it.label; list.appendChild(o);
        }});
      }}).catch(function () {{}});
    }}, 200);
  }});
}})();
</script>"""
    return layout(f"页面设置 · {name}", body, user_name=user_name)


def connect(user_name: str, prompt: str) -> HTMLResponse:
    body = f"""<div class="card"><h2>接入 AI 工具</h2>
<p>把下面这段话<b>完整复制</b>，发给你的 AI 工具（Claude Code、Cursor、Codex 等），它会自己完成配置并确认接通。</p>
<textarea id="prompt" readonly rows="16">{escape(prompt)}</textarea>
<p><button id="copy" type="button">复制这段话</button> <span id="copied" class="muted"></span></p>
<p class="warn">这段话里含有你的接入凭证，只显示这一次。它等同于你的身份：不要发到群里或发给别人。
不用了可以在首页「我的接入凭证」里撤销。</p>
<p class="muted">ChatGPT 网页版的连接器从 OpenAI 云端访问，连不到公司内网，请改用 Codex（或 ChatGPT 桌面 App 里的 Codex）。
Claude Desktop 同理，请用首页「命令行客户端」的方式。</p>
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
