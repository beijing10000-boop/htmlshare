from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from conftest import Env, login, publish, title_of
from htmlshare_server.app import STORAGE_SHIM, inject_shim


def test_inject_shim_position():
    s = STORAGE_SHIM
    assert inject_shim(b"<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>x") == (
        b"<!DOCTYPE html><html lang=zh><head>" + s + b"<meta charset=utf-8>x"
    )
    assert inject_shim(b"<!doctype html><header>x</header>") == b"<!doctype html>" + s + b"<header>x</header>"
    assert inject_shim(b"\xef\xbb\xbf<p>x</p>") == b"\xef\xbb\xbf" + s + b"<p>x</p>"


def test_view_requires_login(env):
    r = env.browser().get("/report.html")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?next=%2Freport.html"


def test_login_returns_to_page(env):
    c = env.browser()
    r = c.get("/auth/login", params={"next": "/report.html"})
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/auth/callback", params={"state": state, "email": "bob@example.com"})
    assert r.headers["location"] == "/report.html"


def test_open_redirect_blocked(env):
    c = env.browser()
    for bad in ["//evil.com", "https://evil.com", "/\\evil.com"]:
        r = c.get("/auth/login", params={"next": bad})
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        r = c.get("/auth/callback", params={"state": state, "email": "bob@example.com"})
        assert r.headers["location"] == "/"


def test_callback_rejects_wrong_state(env):
    c = env.browser()
    c.get("/auth/login")
    r = c.get("/auth/callback", params={"state": "forged", "email": "bob@example.com"})
    assert r.status_code == 400
    assert "hs_session" not in c.cookies


def test_publish_and_acl(env):
    api = env.api("ou_alice")
    r = publish(api, "Q3-Report.html", viewers="bob@example.com, 13800000000")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "q3-report"
    assert body["url"] == "http://testserver/q3-report.html"
    assert body["title"] == "T"
    assert {v["value"] for v in body["viewers"]} == {"ou_bob", "ou_dave"}
    assert body["unresolved"] == []
    assert body["created"] is True

    bob = env.browser("bob@example.com")
    r = bob.get("/q3-report.html")
    assert r.status_code == 200
    assert r.content == b"<html>" + STORAGE_SHIM + b"<title>T</title><body>hi</body></html>"
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in r.headers["content-security-policy"]
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "private, no-store"

    carol = env.browser("carol@example.com")
    r = carol.get("/q3-report.html")
    assert r.status_code == 403
    assert title_of(r) == "没有访问权限"

    owner = env.browser("alice@example.com")
    assert owner.get("/q3-report.html").status_code == 200


def test_all_viewers(env):
    publish(env.api("ou_alice"), "everyone", viewers="all")
    assert env.browser("carol@example.com").get("/everyone.html").status_code == 200


def test_new_page_without_viewers_is_owner_only(env):
    publish(env.api("ou_alice"), "private")
    assert env.browser("bob@example.com").get("/private.html").status_code == 403
    assert env.browser("alice@example.com").get("/private.html").status_code == 200


def test_unresolved_viewers(env):
    r = publish(env.api("ou_alice"), "p1", viewers="bob@example.com,nobody@example.com,dept:不存在的部门,张三")
    body = r.json()
    assert [v["value"] for v in body["viewers"]] == ["ou_bob"]
    assert [u["input"] for u in body["unresolved"]] == ["nobody@example.com", "dept:不存在的部门", "张三"]
    assert "通讯录还在同步" in body["unresolved"][2]["reason"]  # 测试里没跑同步


def test_overwrite_keeps_acl_and_blocks_others(env):
    alice = env.api("ou_alice")
    publish(alice, "doc", viewers="bob@example.com")
    r = publish(alice, "doc", html="<title>v2</title>v2")
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert [v["value"] for v in r.json()["viewers"]] == ["ou_bob"]
    assert "v2" in env.browser("bob@example.com").get("/doc.html").text

    r = publish(env.api("ou_bob"), "doc")
    assert r.status_code == 409


def test_invalid_names(env):
    api = env.api("ou_alice")
    for bad in ["../x", "-a", "a-", "a b", "中文", "a" * 65, ""]:
        assert publish(api, bad).status_code in (400, 422), bad


def test_rejects_binary_and_empty(env):
    api = env.api("ou_alice")
    assert publish(api, "bin", html="\x00\x01\x02").status_code == 400
    assert publish(api, "empty", html="").status_code == 400


def test_non_utf8_served_without_charset(env):
    api = env.api("ou_alice")
    r = api.post(
        "/api/pages",
        data={"name": "gbk"},
        files={"file": ("x.html", '<meta charset="gbk"><title>报告</title>'.encode("gbk"), "text/html")},
    )
    assert r.json()["title"] == "报告"
    r = env.browser("alice@example.com").get("/gbk.html")
    assert r.headers["content-type"] == "text/html"


def test_expired_page(env):
    publish(env.api("ou_alice"), "old", viewers="all")
    with env.db.conn() as c:
        c.execute("UPDATE pages SET expires_at=1 WHERE name='old'")
    assert env.browser("bob@example.com").get("/old.html").status_code == 404
    env.api("ou_alice").get("/api/pages")  # 触发清理
    assert env.db.get_page("old") is None
    assert not (env.settings.data_dir / "pages" / "old.html").exists()


def test_expires_days(env):
    api = env.api("ou_alice")
    assert publish(api, "never", expires_days=0).json()["expires_at"] is None
    assert publish(api, "toolong", expires_days=9999).status_code == 400


def test_update_viewers(env):
    alice = env.api("ou_alice")
    publish(alice, "doc", viewers="bob@example.com")
    r = alice.patch("/api/pages/doc", json={"add_viewers": ["carol@example.com"], "remove_viewers": ["bob@example.com"]})
    assert r.status_code == 200
    assert [v["value"] for v in r.json()["viewers"]] == ["ou_carol"]
    assert env.browser("bob@example.com").get("/doc.html").status_code == 403
    assert env.browser("carol@example.com").get("/doc.html").status_code == 200

    r = alice.patch("/api/pages/doc", json={"set_viewers": ["all"]})
    assert [v["kind"] for v in r.json()["viewers"]] == ["all"]
    r = alice.patch("/api/pages/doc", json={"remove_viewers": ["全员"]})
    assert r.json()["viewers"] == []

    assert env.api("ou_bob").patch("/api/pages/doc", json={"set_viewers": ["all"]}).status_code == 403


def test_delete(env):
    alice = env.api("ou_alice")
    publish(alice, "doc", viewers="all")
    assert env.api("ou_bob").delete("/api/pages/doc").status_code == 403
    assert alice.delete("/api/pages/doc").status_code == 200
    assert env.browser("bob@example.com").get("/doc.html").status_code == 404


def test_list_pages_only_mine_with_views(env):
    publish(env.api("ou_alice"), "a1", viewers="all")
    publish(env.api("ou_bob"), "b1")
    env.browser("carol@example.com").get("/a1.html")
    pages = env.api("ou_alice").get("/api/pages").json()["pages"]
    assert [p["name"] for p in pages] == ["a1"]
    assert pages[0]["views"] == 1


def test_api_requires_token(env):
    c = env.browser()
    assert c.get("/api/pages").status_code == 401
    assert c.get("/api/pages", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_cli_device_login(env):
    c = env.browser()
    start = c.post("/api/cli/start").json()
    assert c.post("/api/cli/poll", json={"device_code": start["device_code"]}).json() == {"status": "pending"}

    browser = env.browser()
    verify_path = start["verification_url"].replace("http://testserver", "")
    r = browser.get(verify_path)
    assert r.status_code == 302  # 需要先登录
    login(browser, "alice@example.com", next_path=verify_path)
    r = browser.get(verify_path)
    assert r.status_code == 200 and start["user_code"] in r.text
    r = browser.post("/cli/verify", data={"user_code": start["user_code"]}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text

    r = c.post("/api/cli/poll", json={"device_code": start["device_code"]}).json()
    assert r["status"] == "ok" and r["user"] == "alice"
    me = c.get("/api/me", headers={"Authorization": f"Bearer {r['token']}"})
    assert me.json()["open_id"] == "ou_alice"
    # token 只能取一次
    assert c.post("/api/cli/poll", json={"device_code": start["device_code"]}).status_code == 404
    # 同一个 user_code 不能再次授权
    r = browser.post("/cli/verify", data={"user_code": start["user_code"]})
    assert r.status_code == 404


def test_cli_verify_rejects_foreign_origin(env):
    start = env.browser().post("/api/cli/start").json()
    alice = env.browser("alice@example.com")
    r = alice.post("/cli/verify", data={"user_code": start["user_code"]}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403


def test_revoke_token(env):
    api = env.api("ou_alice")
    assert api.post("/api/tokens/revoke").status_code == 200
    assert api.get("/api/me").status_code == 401


def test_web_token_and_home(env):
    publish(env.api("ou_alice"), "doc", viewers="bob@example.com")
    alice = env.browser("alice@example.com")
    r = alice.get("/")
    assert r.status_code == 200 and "doc" in r.text and "bob@example.com" in r.text
    r = alice.post("/me/tokens", headers={"Origin": "http://testserver"})
    token = r.text.split("HTMLSHARE_TOKEN=")[1].split("<")[0].strip()
    assert env.browser().get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_tenant_restriction(tmp_path):
    env = Env(tmp_path, allowed_tenant_key="real-tenant")
    c = env.browser()
    r = c.get("/auth/login")
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/auth/callback", params={"state": state, "email": "bob@example.com"})
    assert r.status_code == 403


def test_client_wheel_download(env):
    dist = env.settings.client_dist
    dist.mkdir()
    (dist / "htmlshare-0.1.0-py3-none-any.whl").write_bytes(b"PK")
    c = env.browser()
    assert c.get("/client/htmlshare-0.1.0-py3-none-any.whl").status_code == 200
    assert c.get("/client/other.whl").status_code == 404
    assert "pip install http://testserver/client/htmlshare-0.1.0-py3-none-any.whl" in env.browser("bob@example.com").get("/").text
