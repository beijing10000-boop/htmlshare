"""网页控制台：外壳和安全头、概览统计、我发布的（含网页上传）、访客、访问申请、操作审计、管理员页面。"""

from __future__ import annotations

import re
import time

import pytest

from conftest import Env, publish

ORIGIN = {"Origin": "http://testserver"}
CONSOLE_PAGES = ["/", "/me/pages", "/shared", "/requests", "/connect", "/audit"]


def upload(c, html="<title>网页上传</title>hi", filename="Q3 Report.html", **form):
    return c.post("/me/pages", data=form, files={"file": (filename, html.encode(), "text/html")}, headers=ORIGIN)


def ask(c, page, reason=""):
    return c.post("/me/requests", data={"page": page, "reason": reason}, headers=ORIGIN)


def decide(c, env, page, who, action, next_path=""):
    rid = env.db.get_request(page, who)["id"]
    return c.post(f"/requests/{rid}", data={"action": action, "next": next_path}, headers=ORIGIN)


def actions(env, **where):
    """审计里的 (动作, 页面, 来源)，新的在前；where 按列过滤。"""
    with env.db.conn() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
    return [(r["action"], r["page"], r["via"]) for r in rows if all(r[k] == v for k, v in where.items())]


# ---------- 外壳、静态资源、安全头 ----------
def test_every_console_page_renders_in_the_shell(env):
    publish(env.api("ou_alice"), "doc", viewers="bob@example.com")
    alice = env.browser("alice@example.com")
    for path in [*CONSOLE_PAGES, "/me/pages/doc"]:
        r = alice.get(path)
        assert r.status_code == 200, path
        assert '<aside class="sidebar">' in r.text and "/static/ui-kit.css?v=" in r.text and "/static/app.js?v=" in r.text, path
        assert r.text.count('class="nav-link active"') == 1, path  # 当前所在的导航项高亮，且只有一个
        assert r.headers["cache-control"] == "no-store"
    assert "全站页面" not in alice.get("/").text  # 不是管理员，看不到管理入口


def test_console_pages_have_a_strict_csp_and_no_inline_code(env):
    publish(env.api("ou_alice"), "doc", viewers="bob@example.com")
    alice = env.browser("alice@example.com")
    alice.post("/me/connect", headers=ORIGIN)
    pages = [alice.get(p) for p in [*CONSOLE_PAGES, "/me/pages/doc"]]
    pages += [env.browser("carol@example.com").get("/doc.html"), env.browser().get("/auth/logout")]
    for r in pages:
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["x-frame-options"] == "DENY"
        # 策略不允许内联脚本、内联样式和事件属性；页面里要是有，就会被浏览器悄悄拦掉
        assert not re.search(r"<script(?![^>]*\bsrc=)", r.text), r.url
        assert not re.search(r"<style|\sstyle=|\son[a-z]+=", r.text), r.url


def test_static_assets(env):
    c = env.browser()  # 不用登录
    for name, kind in [("ui-kit.css", "text/css"), ("ui-kit.js", "javascript"), ("app.css", "text/css"), ("app.js", "javascript"), ("favicon.svg", "image/svg+xml")]:
        r = c.get(f"/static/{name}")
        assert r.status_code == 200 and kind in r.headers["content-type"], name
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/static/../app.py").status_code == 404
    # 页面名叫 static 也不冲突：页面在 /static.html，资源在 /static/…
    publish(env.api("ou_alice"), "static", viewers="all")
    assert env.browser("bob@example.com").get("/static.html").status_code == 200


def test_logged_out_pages_redirect_to_login(env):
    for path in [*CONSOLE_PAGES, "/admin/pages", "/admin/status"]:
        r = env.browser().get(path)
        assert r.status_code == 302 and r.headers["location"].startswith("/auth/login?next="), path
    r = env.browser().get("/auth/logout")
    assert "已退出" in r.text and 'href="/auth/login"' in r.text


# ---------- 概览与统计 ----------
def test_views_do_not_count_the_owner(env):
    publish(env.api("ou_alice"), "doc", viewers="bob@example.com,dave@example.com")
    alice, bob, dave, carol = (env.browser(f"{n}@example.com") for n in ("alice", "bob", "dave", "carol"))
    for _ in range(3):
        alice.get("/doc.html")  # 发布者自己预览
    bob.get("/doc.html")
    bob.get("/doc.html")
    dave.get("/doc.html")
    carol.get("/doc.html")  # 不在名单里，被拒绝
    page = env.api("ou_alice").get("/api/pages").json()["pages"][0]
    assert (page["views"], page["visitors"]) == (3, 2) and page["last_view_at"]
    stats = env.api("ou_alice").get("/api/pages/doc/stats").json()
    assert (stats["views"], stats["visitors"], stats["denied"]) == (3, 2, 1)
    assert stats["last_7_days"] == {"views": 3, "visitors": 2, "denied": 1}
    assert len(stats["daily"]) == 14 and stats["daily"][-1]["views"] == 3 and sum(d["views"] for d in stats["daily"]) == 3
    people = {p["name"]: p for p in stats["people"]}
    assert set(people) == {"bob", "dave", "carol"}  # 没有发布者自己
    assert people["bob"]["views"] == 2 and not people["bob"]["blocked"]
    assert people["carol"]["denied"] == 1 and people["carol"]["blocked"]
    assert env.api("ou_bob").get("/api/pages/doc/stats").status_code == 403  # 只有发布者能看访客


def test_overview_numbers(env):
    publish(env.api("ou_alice"), "doc", html="<title>周报</title>", viewers="bob@example.com")
    publish(env.api("ou_alice"), "open", viewers="all")
    publish(env.api("ou_bob"), "from-bob", html="<title>bob 的页面</title>", viewers="alice@example.com")
    with env.db.conn() as c:
        c.execute("UPDATE pages SET expires_at=? WHERE name='open'", (int(time.time()) + 3 * 86400 - 3600,))
    bob, carol = env.browser("bob@example.com"), env.browser("carol@example.com")
    bob.get("/doc.html")
    carol.get("/doc.html")
    ask(carol, "doc", "想看周报")
    o = env.app.state.service.overview("ou_alice")
    assert len(o["pages"]) == 2 and o["all_count"] == 1
    assert (o["week"]["views"], o["week"]["visitors"], o["week"]["denied_people"]) == (1, 1, 1)
    assert [p["name"] for p in o["expiring"]] == ["open"]
    assert [r["name"] for r in o["pending"]] == ["carol"]
    assert [p["name"] for p in o["shared"]] == ["from-bob"]
    assert [(a["name"], a["allowed"]) for a in o["recent"]] == [("carol", False), ("bob", True)]

    home = env.browser("alice@example.com").get("/").text
    assert "想看周报" in home and "剩 2 天" in home and "bob 的页面" in home
    assert '<span class="nav-badge" title="1 个待处理">1</span>' in home
    assert "data-chart=" in home  # 有访问才画趋势图
    assert "最近 14 天还没有人打开过你发布的页面" in env.browser("bob@example.com").get("/").text


def test_deleting_a_page_clears_its_access_log(env):
    # 页面名以后可能被别人用：旧页面的访客不能出现在新页面的统计里
    publish(env.api("ou_alice"), "reuse", viewers="all")
    env.browser("carol@example.com").get("/reuse.html")
    env.api("ou_alice").delete("/api/pages/reuse")
    publish(env.api("ou_bob"), "reuse")
    stats = env.api("ou_bob").get("/api/pages/reuse/stats").json()
    assert stats["views"] == 0 and stats["people"] == []


# ---------- 我发布的：列表、网页上传、访客 ----------
def test_pages_list_has_filter_tags(env):
    alice = env.api("ou_alice")
    publish(alice, "p-all", viewers="all")
    publish(alice, "p-scoped", viewers="bob@example.com")
    publish(alice, "p-private")
    text = env.browser("alice@example.com").get("/me/pages").text
    tags = dict(re.findall(r'<tr data-tags="([a-z ]+)">\s*<td>\s*<a class="cell-title" href="/me/pages/([a-z-]+)"', text))
    assert {v: k for k, v in tags.items()} == {"p-all": "all", "p-scoped": "scoped", "p-private": "private"}
    assert "还没有发布过页面" in env.browser("bob@example.com").get("/me/pages").text


def test_web_upload(env):
    alice = env.browser("alice@example.com")
    r = upload(alice, viewers="bob@example.com, nobody@example.com", days="7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["created"] and body["name"] == "q3-report"  # 没填页面名，用文件名
    assert body["redirect"] == "/me/pages/q3-report?done=published"
    assert [u["input"] for u in body["unresolved"]] == ["nobody@example.com"]
    assert env.browser("bob@example.com").get("/q3-report.html").status_code == 200
    assert "已发布" in alice.get(body["redirect"]).text

    # 同名再传 = 覆盖更新，不填可见人就保留原名单
    body = upload(alice, html="<title>第二版</title>v2", name="q3-report").json()
    assert body["ok"] and not body["created"] and body["redirect"].endswith("?done=updated")
    assert "v2" in env.browser("bob@example.com").get("/q3-report.html").text
    # 中文文件名凑不出页面名，自动生成
    assert upload(alice, filename="三季度报告.html").json()["name"].startswith("page-")
    assert actions(env, action="publish")[-1] == ("publish", "q3-report", "web")


def test_web_upload_errors_come_back_as_json(env):
    publish(env.api("ou_bob"), "taken")
    alice = env.browser("alice@example.com")
    for form, status, words in [
        ({"name": "Bad Name"}, 400, "页面名只能用"),
        ({"name": "taken"}, 409, "已被"),
        ({"name": "ok", "days": "9999"}, 400, "过期天数必须在"),
        ({"name": "ok", "days": "abc"}, 400, "过期天数要填数字"),
    ]:
        r = upload(alice, **form)
        assert r.status_code == status and r.json()["ok"] is False and words in r.json()["error"], form
    assert upload(alice, html="", name="ok").status_code == 400
    assert upload(env.browser(), name="ok").status_code == 401
    r = alice.post("/me/pages", data={"name": "ok"}, files={"file": ("a.html", b"x", "text/html")}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403 and env.db.get_page("ok") is None


def test_page_detail_shows_visitors_and_lets_owner_add_blocked_ones(env):
    env.user_dir.sync()
    publish(env.api("ou_alice"), "doc", html="<title>周报</title>", viewers="bob@example.com")
    alice, bob, carol = (env.browser(f"{n}@example.com") for n in ("alice", "bob", "carol"))
    bob.get("/doc.html")
    carol.get("/doc.html")
    text = alice.get("/me/pages/doc").text
    visitors = text.split('id="visitors"')[1].split('id="manage"')[0]
    assert "bob" in visitors and "carol" in visitors and "能打开" in visitors and "被拒绝" in visitors
    assert 'name="viewers" value="ou_carol"' in visitors and 'name="viewers" value="ou_bob"' not in visitors
    r = alice.post("/me/pages/doc", data={"action": "add", "viewers": "ou_carol"}, headers=ORIGIN)
    assert r.status_code == 303
    assert carol.get("/doc.html").status_code == 200
    visitors = alice.get("/me/pages/doc").text.split('id="visitors"')[1].split('id="manage"')[0]
    assert "被拒绝" not in visitors.split("</thead>")[1]  # carol 最近一次成功了，状态变成能打开


# ---------- 访问申请 ----------
def test_access_request_approve(env):
    publish(env.api("ou_alice"), "doc", html="<title>周报</title>", viewers="bob@example.com")
    alice, carol = env.browser("alice@example.com"), env.browser("carol@example.com")
    r = carol.get("/doc.html")
    assert r.status_code == 403 and 'action="/me/requests"' in r.text and "向 alice 申请访问" in r.text
    r = ask(carol, "doc", "  需要核对\n容量数据  ")
    assert r.status_code == 303 and r.headers["location"] == "/doc.html"
    r = carol.get("/doc.html")
    assert r.status_code == 403 and "已在" in r.text and 'action="/me/requests"' not in r.text  # 等处理时不能重复提交
    assert env.db.get_request("doc", "ou_carol")["reason"] == "需要核对 容量数据"

    page = alice.get("/requests").text
    assert "需要核对 容量数据" in page.split("等我处理")[1].split("我发出的申请")[0]
    assert "等待处理" in carol.get("/requests").text
    assert alice.get("/me/pages/doc").text.count("需要核对 容量数据") == 1

    r = decide(alice, env, "doc", "ou_carol", "approve")
    assert r.status_code == 303 and r.headers["location"] == "/requests?done=approved"
    assert carol.get("/doc.html").status_code == 200
    assert [e.value for e in env.db.get_acl("doc") if e.kind == "user"] == ["ou_bob", "ou_carol"]
    assert "已通过" in carol.get("/requests").text
    assert actions(env, page="doc")[:2] == [("request_approve", "doc", "web"), ("request", "doc", "web")]
    # 处理过的不能再处理一次（另一个标签页里点了两下）
    r = decide(alice, env, "doc", "ou_carol", "reject", next_path="/me/pages/doc")
    assert r.headers["location"] == "/me/pages/doc?done=handled"
    assert carol.get("/doc.html").status_code == 200


def test_access_request_reject_and_ask_again(env):
    publish(env.api("ou_alice"), "doc")
    alice, carol = env.browser("alice@example.com"), env.browser("carol@example.com")
    ask(carol, "doc", "第一次")
    ask(carol, "doc", "重复点")  # 还在等处理：不重复记，也不改理由
    assert len(actions(env, action="request")) == 1 and env.db.get_request("doc", "ou_carol")["reason"] == "第一次"
    assert decide(alice, env, "doc", "ou_carol", "reject").headers["location"] == "/requests?done=rejected"
    r = carol.get("/doc.html")
    assert r.status_code == 403 and "上次的申请没有通过" in r.text and 'action="/me/requests"' in r.text
    ask(carol, "doc", "第二次")
    row = env.db.get_request("doc", "ou_carol")
    assert (row["status"], row["reason"], row["decided_at"]) == ("pending", "第二次", None)


def test_only_the_owner_can_decide(env):
    publish(env.api("ou_alice"), "doc")
    carol, bob = env.browser("carol@example.com"), env.browser("bob@example.com")
    ask(carol, "doc")
    assert decide(bob, env, "doc", "ou_carol", "approve").status_code == 404
    assert decide(carol, env, "doc", "ou_carol", "approve").status_code == 404  # 自己也不能通过自己的
    rid = env.db.get_request("doc", "ou_carol")["id"]
    r = env.browser("alice@example.com").post(f"/requests/{rid}", data={"action": "approve"}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403
    assert env.browser().post(f"/requests/{rid}", data={"action": "approve"}, headers=ORIGIN).status_code == 401
    assert env.db.get_request("doc", "ou_carol")["status"] == "pending"
    assert carol.get("/doc.html").status_code == 403


def test_request_edge_cases(env):
    publish(env.api("ou_alice"), "doc", viewers="bob@example.com")
    bob, alice = env.browser("bob@example.com"), env.browser("alice@example.com")
    # 已经能看的人、发布者自己：不产生申请，直接回到页面
    for c in (bob, alice):
        r = ask(c, "doc")
        assert r.status_code == 303 and r.headers["location"] == "/doc.html"
    assert env.db.get_request("doc", "ou_bob") is None and env.db.get_request("doc", "ou_alice") is None
    assert ask(bob, "nope").status_code == 404
    assert ask(bob, "../etc").status_code == 400
    assert ask(env.browser(), "doc").status_code == 401
    r = env.browser("carol@example.com").post("/me/requests", data={"page": "doc"}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403 and env.db.get_request("doc", "ou_carol") is None


def test_requests_settle_when_the_person_gets_access_another_way(env):
    env.user_dir.sync()
    alice = env.api("ou_alice")
    publish(alice, "by-name")
    publish(alice, "by-all")
    publish(alice, "by-dept")
    carol, bob = env.browser("carol@example.com"), env.browser("bob@example.com")
    ask(carol, "by-name")
    ask(carol, "by-all")
    ask(bob, "by-dept")
    status = lambda page, who: env.db.get_request(page, who)["status"]  # noqa: E731
    # 发布者在别处（AI 工具、接口）直接把人加进名单：申请自动算通过，不会一直挂着
    alice.patch("/api/pages/by-name", json={"add_viewers": ["carol@example.com"]})
    alice.patch("/api/pages/by-all", json={"set_viewers": ["all"]})
    assert status("by-name", "ou_carol") == "approved" and status("by-all", "ou_carol") == "approved"
    # 按部门加进来的，发布时不知道里面有谁，等他下次打开成功时结掉
    alice.patch("/api/pages/by-dept", json={"add_viewers": ["dept:技术中心/运维部"]})
    assert status("by-dept", "ou_bob") == "pending"
    assert bob.get("/by-dept.html").status_code == 200
    assert status("by-dept", "ou_bob") == "approved"
    assert env.app.state.service.owner_requests("ou_alice", pending=True) == []


def test_notifications_are_off_by_default_and_sent_when_enabled(tmp_path):
    quiet = Env(tmp_path / "quiet")
    publish(quiet.api("ou_alice"), "doc")
    ask(quiet.browser("carol@example.com"), "doc")
    assert quiet.feishu.sent == []

    env = Env(tmp_path / "loud", notify=True, admins=("ou_dave",))
    env.app.state.service.spawn = lambda fn: fn()  # 测试里同步发，不起线程
    publish(env.api("ou_alice"), "doc", html="<title>周报</title>")
    alice, carol = env.browser("alice@example.com"), env.browser("carol@example.com")
    ask(carol, "doc", "想看")
    assert env.feishu.sent == [("ou_alice", "carol 申请查看你发布的页面「周报」，理由：想看。去处理：http://testserver/requests")]
    decide(alice, env, "doc", "ou_carol", "reject")
    ask(carol, "doc")
    decide(alice, env, "doc", "ou_carol", "approve")
    assert [who for who, _ in env.feishu.sent] == ["ou_alice", "ou_carol", "ou_alice", "ou_carol"]
    assert "没有通过" in env.feishu.sent[1][1] and "已通过" in env.feishu.sent[3][1] and "/doc.html" in env.feishu.sent[3][1]
    # 管理员下线别人的页面也通知发布者
    env.browser("dave@example.com").post("/admin/pages/doc", data={"action": "delete"}, headers=ORIGIN)
    assert env.feishu.sent[-1][0] == "ou_alice" and "下线了你发布的页面「周报」" in env.feishu.sent[-1][1]

    # 飞书发不出去不影响申请本身
    def boom(open_id, text):
        raise RuntimeError("飞书挂了")

    env.feishu.send_text = boom
    publish(env.api("ou_alice"), "doc2")
    assert ask(carol, "doc2").status_code == 303 and env.db.get_request("doc2", "ou_carol")["status"] == "pending"


# ---------- 操作审计 ----------
def test_audit_records_who_did_what_and_from_where(env):
    alice_api = env.api("ou_alice")
    publish(alice_api, "doc", viewers="bob@example.com")
    alice_api.patch("/api/pages/doc", json={"add_viewers": ["carol@example.com"], "remove_viewers": ["bob@example.com"]})
    alice_api.patch("/api/pages/doc", json={"add_viewers": ["carol@example.com"]})  # 没变化，不记
    alice = env.browser("alice@example.com")
    alice.post("/me/pages/doc", data={"action": "expiry", "days": "0"}, headers=ORIGIN)
    alice.post("/me/connect", headers=ORIGIN)
    alice.post("/me/pages/doc", data={"action": "delete"}, headers=ORIGIN)
    assert actions(env, actor="ou_alice")[::-1] == [
        ("publish", "doc", "api"),
        ("viewers", "doc", "api"),
        ("login", "", "web"),
        ("expiry", "doc", "web"),
        ("token_create", "", "web"),
        ("delete", "doc", "web"),
    ]
    with env.db.conn() as c:
        detail = {r["action"]: r["detail"] for r in c.execute("SELECT action, detail FROM audit_log")}
    assert detail["viewers"] == "新增 carol@example.com；移除 bob@example.com"
    assert detail["publish"] == "新页面，1 KB，可见：bob@example.com" and detail["expiry"] == "改为永不过期"

    text = alice.get("/audit").text
    for words in ("发布页面", "修改可见范围", "修改过期时间", "删除页面", "生成凭证", "接口 / 命令行", "网页"):
        assert words in text, words


def test_expired_pages_are_purged_by_the_system_and_owner_sees_it(env):
    publish(env.api("ou_alice"), "old")
    with env.db.conn() as c:
        c.execute("UPDATE pages SET expires_at=1 WHERE name='old'")
    env.api("ou_bob").get("/api/pages")  # 任何人的请求都会触发清理
    assert actions(env, action="expire") == [("expire", "old", "system")]
    text = env.browser("alice@example.com").get("/audit").text
    assert "到期清理" in text and "系统" in text


def test_audit_shows_only_what_concerns_me(env):
    publish(env.api("ou_alice"), "alice-page")
    publish(env.api("ou_bob"), "bob-page")
    ask(env.browser("carol@example.com"), "alice-page", "给我看看")
    alice, bob = env.browser("alice@example.com"), env.browser("bob@example.com")
    mine = alice.get("/audit").text
    assert "alice-page" in mine and "给我看看" in mine  # 别人对我的页面提的申请，我看得到
    assert "bob-page" not in mine
    assert "alice-page" not in bob.get("/audit").text
    assert "bob-page" not in alice.get("/audit?scope=all").text  # 不是管理员，scope=all 不起作用


def test_audit_paging(env):
    for i in range(105):
        env.db.add_audit("ou_alice", "publish", f"p{i}", "ou_alice", "", "api")
    alice = env.browser("alice@example.com")  # 登录本身也记一条，所以第一页是「登录」+ p104…p6
    first = alice.get("/audit").text
    older = re.search(r'href="/audit\?before=(\d+)"', first).group(1)
    assert ">p104<" in first and ">p6<" in first and ">p5<" not in first and "回到最新" not in first
    second = alice.get(f"/audit?before={older}").text
    assert ">p5<" in second and ">p0<" in second and "更早的记录" not in second and "回到最新" in second


# ---------- 管理员 ----------
@pytest.fixture
def admin_env(tmp_path):
    env = Env(tmp_path, admins=("ou_dave", "erin@example.com"))
    publish(env.api("ou_alice"), "alice-page", html="<title>爱丽丝的页面</title>", viewers="all")
    publish(env.api("ou_bob"), "bob-page", html="<title>鲍勃的页面</title>")
    return env


def test_admin_pages_need_admin(admin_env):
    alice = admin_env.browser("alice@example.com")
    for path in ("/admin/pages", "/admin/status"):
        r = alice.get(path)
        assert r.status_code == 403 and "需要管理员权限" in r.text, path
    r = alice.post("/admin/pages/bob-page", data={"action": "delete"}, headers=ORIGIN)
    assert r.status_code == 403 and admin_env.db.get_page("bob-page")
    assert alice.post("/admin/status", data={"action": "sync"}, headers=ORIGIN).status_code == 403


def test_admin_sees_every_page_but_not_its_content(admin_env):
    dave = admin_env.browser("dave@example.com")  # 按 open_id 配的管理员
    text = dave.get("/admin/pages").text
    assert "爱丽丝的页面" in text and "鲍勃的页面" in text and "全站页面" in text
    assert dave.get("/bob-page.html").status_code == 403  # 管理员也要在可见范围里才能看内容
    assert "bob-page" in dave.get("/audit?scope=all").text and "bob-page" not in dave.get("/audit").text
    erin = admin_env.browser("erin@example.com")  # 按邮箱配的管理员
    assert erin.get("/admin/pages").status_code == 200


def test_admin_take_down(admin_env):
    dave = admin_env.browser("dave@example.com")
    r = dave.post("/admin/pages/bob-page", data={"action": "delete"}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403 and admin_env.db.get_page("bob-page")
    r = dave.post("/admin/pages/bob-page", data={"action": "delete"}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/admin/pages?done=taken_down"
    assert admin_env.db.get_page("bob-page") is None
    assert not (admin_env.settings.data_dir / "pages" / "bob-page.html").exists()
    assert actions(admin_env, action="admin_delete", actor="ou_dave", owner="ou_bob") == [("admin_delete", "bob-page", "web")]
    assert "管理员下线" in admin_env.browser("bob@example.com").get("/audit").text  # 发布者自己查得到
    assert dave.post("/admin/pages/bob-page", data={"action": "delete"}, headers=ORIGIN).status_code == 404


def test_admin_status(admin_env):
    admin_env.user_dir.sync()
    dave = admin_env.browser("dave@example.com")
    text = dave.get("/admin/status").text
    assert "开发模式" in text and "7 个" in text
    admins = text.split("<h2>管理员</h2>")[1]
    assert "<strong>dave</strong>" in admins  # 按 open_id 配的，显示名字
    assert "erin@example.com" in admins and "还没登录过" in admins  # 按邮箱配、还没登录过的，只有邮箱
    st = admin_env.app.state.service.system_status()
    assert (st["pages"], st["owners"], st["departments"], st["directory"]) == (2, 2, 7, 5)
    assert st["db_bytes"] > 0 and st["directory_ts"]
    r = dave.post("/admin/status", data={"action": "sync"}, headers=ORIGIN)
    assert r.headers["location"] == "/admin/status?done=sync"
    assert admin_env.user_dir._wake.is_set()
    assert actions(admin_env, action="directory_sync") == [("directory_sync", "", "web")]


# ---------- 升级 ----------
def test_upgrade_from_v4_drops_access_rows_left_by_deleted_pages(tmp_path):
    import sqlite3

    from htmlshare_server.db import Database

    env = Env(tmp_path)
    publish(env.api("ou_bob"), "reused", viewers="all")
    publish(env.api("ou_bob"), "kept", viewers="all")
    env.browser("carol@example.com").get("/kept.html")
    created = env.db.get_page("reused").created_at
    path = tmp_path / "htmlshare.db"
    with sqlite3.connect(path) as c:  # 模拟 0.6.0 留下的库：删掉的页面、被别人重新用了名字的页面，访问记录都还在
        c.execute("INSERT INTO access_log(page, open_id, at, allowed) VALUES('gone', 'ou_carol', ?, 1)", (created,))
        c.execute("INSERT INTO access_log(page, open_id, at, allowed) VALUES('reused', 'ou_carol', ?, 1)", (created - 3600,))
        c.execute("PRAGMA user_version=4")
    db = Database(path)
    with db.conn() as c:
        assert [r["page"] for r in c.execute("SELECT page FROM access_log")] == ["kept"]
        assert c.execute("PRAGMA user_version").fetchone()[0] == 5
        c.execute("INSERT INTO access_log(page, open_id, at, allowed) VALUES('gone', 'ou_carol', 1, 1)")
    Database(path)  # 已经是 v5，不再清
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 2
