"""远程 MCP：真实 uvicorn + 真实 MCP HTTP 客户端，走完整链路（lifespan、鉴权、Host 校验、工具、一次性上传）。"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from conftest import Env, publish
from htmlshare_server.app import hash_secret
from htmlshare_server.remote_mcp import connect_prompt

TOKEN = "hs_test_ou_alice"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
    port = _free_port()
    env = Env(tmp_path, public_url=f"http://127.0.0.1:{port}", directory_sync=False)
    env.user_dir.sync()
    env.api("ou_alice")  # 生成 TOKEN
    server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    env.base = f"http://127.0.0.1:{port}"
    yield env
    server.should_exit = True
    t.join(5)


def mcp_session(base: str, token: str = TOKEN):
    """返回一个 async 函数：在一个 MCP 会话里依次执行 calls=[(tool, args), ...]。"""

    async def run(calls):
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as (r, w):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    out = {"instructions": init.instructions, "tools": [t.name for t in (await s.list_tools()).tools]}
                    results = []
                    for tool, args in calls:
                        res = await s.call_tool(tool, args)
                        text = "".join(getattr(c, "text", "") for c in res.content)
                        results.append((res.is_error, json.loads(text) if not res.is_error else text))
                    out["results"] = results
                    return out

    return lambda calls: asyncio.run(run(calls))


def test_tools_and_instructions(live):
    out = mcp_session(live.base)([("whoami", {})])
    assert set(out["tools"]) == {
        "prepare_upload", "publish_html", "update_viewers", "list_my_pages", "list_shared_with_me",
        "delete_page", "find_user", "whoami",
    }
    assert "prepare_upload" in out["instructions"]
    ok, me = out["results"][0]
    assert not ok and me["open_id"] == "ou_alice"


def test_publish_html_and_view(live):
    out = mcp_session(live.base)([
        ("publish_html", {"html_content": "<title>MCP</title>hi", "name": "via-mcp", "viewers": ["戴维", "张三"]}),
        ("list_my_pages", {}),
    ])
    (err, page), (_, listing) = out["results"]
    assert not err
    assert page["url"] == f"{live.base}/via-mcp.html"
    assert page["viewers"] == ["戴维 <dave@example.com>"]
    assert "有 2 个人叫“张三”" in page["unresolved"][0]["reason"]
    assert [p["name"] for p in listing["pages"]] == ["via-mcp"]
    assert live.browser("dave@example.com").get("/via-mcp.html").status_code == 200


def test_prepare_upload_then_put(live):
    out = mcp_session(live.base)([("prepare_upload", {"name": "Q3-Report", "viewers": ["dept:研发部"], "expires_days": 7})])
    err, ticket = out["results"][0]
    assert not err
    assert ticket["name"] == "q3-report"
    assert ticket["page_url"] == f"{live.base}/q3-report.html"
    assert ticket["viewers"] == ["部门:技术中心/研发部"]
    assert 'curl.exe -sS -T "<HTML文件路径>"' in ticket["how_to_upload"]["Windows PowerShell"]
    url = ticket["upload_url"]
    assert "hs_" not in url  # 上传地址里不出现长期 token

    # 内容不合格：报错但不作废地址
    assert httpx.put(url, content=b"").status_code == 400
    r = httpx.put(url, content="<title>三季度</title>ok".encode())  # 等价于 curl -T
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == f"{live.base}/q3-report.html" and body["title"] == "三季度"
    assert body["message"].startswith("已发布：")
    assert [v["label"] for v in body["viewers"]] == ["部门:技术中心/研发部"]
    assert body["expires_at"] is not None
    # 只能用一次
    again = httpx.put(url, content=b"<p>x</p>")
    assert again.status_code == 404 and "只能用一次" in again.json()["detail"]
    assert live.browser("dave@example.com").get("/q3-report.html").status_code == 200  # dave 在研发部


def test_tool_errors_reach_the_model(live):
    out = mcp_session(live.base)([
        ("publish_html", {"html_content": "<p>x</p>", "name": "Bad Name"}),
        ("delete_page", {"name": "nope"}),
        ("prepare_upload", {"name": "ok", "expires_days": 9999}),
    ])
    (e1, m1), (e2, m2), (e3, m3) = out["results"]
    assert e1 and "页面名只能用小写字母" in m1
    assert e2 and "不存在" in m2
    assert e3 and "过期天数必须在" in m3


def test_update_find_delete(live):
    publish(live.api("ou_alice"), "doc", viewers="bob@example.com")
    out = mcp_session(live.base)([
        ("update_viewers", {"name": "doc", "add": ["dept:研发部"], "remove": ["张三"]}),
        ("find_user", {"query": "dave"}),
        ("delete_page", {"name": "doc"}),
    ])
    (_, page), (_, users), (_, deleted) = out["results"]
    assert page["viewers"] == ["部门:技术中心/研发部"]
    assert [u["email"] for u in users["users"]] == ["dave@example.com"]
    assert deleted == {"ok": True, "name": "doc"}


def test_list_shared_with_me(live):
    publish(live.api("ou_bob"), "from-bob", html="<title>bob 给大家</title>", viewers="all")
    publish(live.api("ou_bob"), "bob-private", html="<title>私有</title>")
    out = mcp_session(live.base)([("list_shared_with_me", {})])
    err, res = out["results"][0]
    assert not err
    assert res["pages"] == [
        {
            "url": f"{live.base}/from-bob.html",
            "name": "from-bob",
            "title": "bob 给大家",
            "owner_name": "张三",  # bob 没在浏览器登录过，名字来自通讯录
            "via": "全员可见",
            "updated_at": res["pages"][0]["updated_at"],
            "expires_at": res["pages"][0]["expires_at"],
        }
    ]
    assert "note" not in res


def test_mcp_requires_valid_token(live):
    r = httpx.post(f"{live.base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "接入 AI 工具" in r.json()["detail"]
    r = httpx.post(f"{live.base}/mcp", json={}, headers={"Authorization": "Bearer hs_nope"})
    assert r.status_code == 401
    with pytest.raises(Exception):
        mcp_session(live.base, token="hs_revoked")([("whoami", {})])


def test_revoked_token_stops_working(live):
    live.db.revoke_token(hash_secret(TOKEN))
    r = httpx.post(f"{live.base}/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_rejects_foreign_host_header(live):
    # DNS 重绑定防护：只认 PUBLIC_URL 的主机名和本机
    r = httpx.post(
        f"{live.base}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example.com", "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code in (400, 403, 421)


def test_upload_ticket_edge_cases(tmp_path):
    env = Env(tmp_path)
    svc = env.app.state.service
    c = TestClient(env.app)
    assert c.put("/api/uploads/not-a-ticket", content=b"<p>x</p>").status_code == 404

    t = svc.prepare_upload("ou_alice", "taken-later")
    publish(env.api("ou_bob"), "taken-later")  # 准备好地址之后，名字被别人抢先占了
    path = t["upload_url"].replace("http://testserver", "")
    r = c.put(path, content=b"<p>x</p>")
    assert r.status_code == 409

    t = svc.prepare_upload("ou_alice", "big")
    big = b"<p>" + b"x" * (env.settings.max_upload_mb * 1024 * 1024) + b"</p>"
    assert c.put(t["upload_url"].replace("http://testserver", ""), content=big).status_code == 413

    with env.db.conn() as conn:
        conn.execute("UPDATE upload_tickets SET expires_at=1")
    assert c.put(t["upload_url"].replace("http://testserver", ""), content=b"<p>x</p>").status_code == 404


def test_connect_page_and_token_management(tmp_path):
    env = Env(tmp_path)
    alice = env.browser("alice@example.com")
    r = alice.post("/me/connect", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert "http://testserver/mcp" in r.text and "claude mcp add --transport http --scope user htmlshare" in r.text
    token = r.text.split("Authorization: Bearer ")[1].split()[0].rstrip("<\"&")
    assert env.db.token_owner(hash_secret(token)) == "ou_alice"

    home = alice.get("/")
    assert "我的接入凭证" in home.text and "AI 接入" in home.text
    token_id = env.db.list_tokens("ou_alice")[0]["id"]
    assert alice.post(f"/me/tokens/{token_id}/revoke", headers={"Origin": "https://evil.com"}).status_code == 403
    assert env.browser("bob@example.com").post(f"/me/tokens/{token_id}/revoke").status_code == 303  # 别人撤不掉
    assert env.db.token_owner(hash_secret(token)) == "ou_alice"
    r = alice.post(f"/me/tokens/{token_id}/revoke")
    assert r.status_code == 303
    assert env.db.token_owner(hash_secret(token)) is None


def test_connect_prompt_escapes_nothing_weird():
    p = connect_prompt("http://share.example.com", "hs_abc")
    assert '"htmlshare": {"url": "http://share.example.com/mcp", "headers": {"Authorization": "Bearer hs_abc"}}' in p
    assert "不要写进项目目录或提交到 git" in p
