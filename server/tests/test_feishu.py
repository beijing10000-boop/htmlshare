from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import make_settings
from htmlshare_server.feishu import FeishuClient, FeishuError


def make_client(tmp_path, handler):
    s = make_settings(tmp_path, dev_fake_login=False, feishu_app_id="cli_test", feishu_app_secret="sec")
    return FeishuClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_authorize_url(tmp_path):
    fs = make_client(tmp_path, lambda r: httpx.Response(500))
    url = fs.authorize_url("http://testserver/auth/callback", "st8")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    q = parse_qs(parts.query)
    assert q == {
        "client_id": ["cli_test"],
        "response_type": ["code"],
        "redirect_uri": ["http://testserver/auth/callback"],
        "state": ["st8"],
    }


def test_login(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == "/open-apis/authen/v2/oauth/token":
            return httpx.Response(200, json={"code": 0, "access_token": "u-abc", "expires_in": 7200})
        if req.url.path == "/open-apis/authen/v1/user_info":
            assert req.headers["authorization"] == "Bearer u-abc"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"open_id": "ou_1", "name": "张三", "enterprise_email": "zs@corp.com", "tenant_key": "tk"}},
            )
        return httpx.Response(404)

    user = make_client(tmp_path, handler).login("the-code", "http://testserver/auth/callback")
    assert (user.open_id, user.name, user.email, user.tenant_key) == ("ou_1", "张三", "zs@corp.com", "tk")
    body = json.loads(seen[0].content)
    assert body == {
        "grant_type": "authorization_code",
        "client_id": "cli_test",
        "client_secret": "sec",
        "code": "the-code",
        "redirect_uri": "http://testserver/auth/callback",
    }


def test_login_error_surfaces_message(tmp_path):
    def handler(req):
        return httpx.Response(400, json={"code": 20050, "error": "invalid_grant", "error_description": "code 已过期"})

    with pytest.raises(FeishuError, match="20050 code 已过期"):
        make_client(tmp_path, handler).login("x", "y")


def test_resolve_batches_and_caches_tenant_token(tmp_path):
    calls = {"token": 0, "batch": 0}

    def handler(req: httpx.Request):
        if req.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            calls["token"] += 1
            assert json.loads(req.content) == {"app_id": "cli_test", "app_secret": "sec"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        if req.url.path == "/open-apis/contact/v3/users/batch_get_id":
            calls["batch"] += 1
            assert req.url.params["user_id_type"] == "open_id"
            assert req.headers["authorization"] == "Bearer t-1"
            body = json.loads(req.content)
            assert len(body["emails"]) <= 50
            users = [{"user_id": f"ou_{e.split('@')[0]}", "email": e} for e in body["emails"] if not e.startswith("x")]
            users += [{"email": e} for e in body["emails"] if e.startswith("x")]
            users += [{"user_id": "ou_m", "mobile": m} for m in body["mobiles"]]
            return httpx.Response(200, json={"code": 0, "data": {"user_list": users}})
        return httpx.Response(404)

    fs = make_client(tmp_path, handler)
    emails = [f"u{i}@corp.com" for i in range(60)] + ["xnobody@corp.com"]
    found = fs.resolve(emails, ["13800000000"])
    assert len(found) == 61
    assert found["u0@corp.com"] == "ou_u0"
    assert found["13800000000"] == "ou_m"
    assert "xnobody@corp.com" not in found
    assert calls == {"token": 1, "batch": 2}
    fs.resolve(["u1@corp.com"], [])
    assert calls["token"] == 1


def _tenant_ok(req):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200})


def test_list_departments_pages_and_skips_deleted(tmp_path):
    pages = {
        None: {"has_more": True, "page_token": "p2", "items": [
            {"open_department_id": "od-a", "name": "技术中心", "parent_department_id": "0"},
            {"open_department_id": "od-x", "name": "已撤销", "parent_department_id": "0", "status": {"is_deleted": True}},
        ]},
        "p2": {"has_more": False, "items": [{"open_department_id": "od-b", "name": "运维部", "parent_department_id": "od-a"}]},
    }

    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        assert req.url.path == "/open-apis/contact/v3/departments/0/children"
        assert req.url.params["fetch_child"] == "true"
        assert req.url.params["department_id_type"] == "open_department_id"
        return httpx.Response(200, json={"code": 0, "data": pages[req.url.params.get("page_token")]})

    depts = make_client(tmp_path, handler).list_departments()
    assert [(d.id, d.name, d.parent_id) for d in depts] == [("od-a", "技术中心", "0"), ("od-b", "运维部", "od-a")]


def test_list_department_users(tmp_path):
    pages = {
        None: {"has_more": True, "page_token": "p2", "items": [
            {"open_id": "ou_1", "name": "张三", "en_name": "Zhang San", "enterprise_email": "ZS@corp.com", "email": "zs@gmail.com",
             "department_ids": ["od-a"], "status": {"is_resigned": False}},
            {"open_id": "ou_2", "name": "离职的人", "department_ids": ["od-a"], "status": {"is_resigned": True}},
        ]},
        "p2": {"has_more": False, "items": [{"open_id": "ou_3", "name": "李四", "department_ids": ["od-a", "od-b"]}]},
    }

    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        assert req.url.path == "/open-apis/contact/v3/users/find_by_department"
        assert req.url.params["department_id"] == "od-a"
        assert req.url.params["user_id_type"] == "open_id"
        return httpx.Response(200, json={"code": 0, "data": pages[req.url.params.get("page_token")]})

    users = make_client(tmp_path, handler).list_department_users("od-a")
    assert [(u.open_id, u.name, u.en_name, u.email, u.dept_ids) for u in users] == [
        ("ou_1", "张三", "Zhang San", "zs@corp.com", ("od-a",)),  # 企业邮箱优先，转小写
        ("ou_3", "李四", "", "", ("od-a", "od-b")),
    ]


def test_list_department_users_requires_name(tmp_path):
    # 真实飞书：没开 contact:user.base:readonly 时成员里没有 name
    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        items = [{"open_id": "ou_1", "union_id": "on_1", "department_ids": ["od-a"]}]
        return httpx.Response(200, json={"code": 0, "data": {"has_more": False, "items": items}})

    with pytest.raises(FeishuError, match="contact:user.base:readonly"):
        make_client(tmp_path, handler).list_department_users("od-a")


def test_list_departments_requires_parent_field(tmp_path):
    # 真实飞书：没开 contact:department.organize:readonly 时，部门里没有 parent_department_id
    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        items = [{"open_department_id": "od-a", "department_id": "a", "name": "运维部", "status": {"is_deleted": False}}]
        return httpx.Response(200, json={"code": 0, "data": {"has_more": False, "items": items}})

    with pytest.raises(FeishuError, match="contact:department.organize:readonly"):
        make_client(tmp_path, handler).list_departments()


def test_user_department_ids(tmp_path):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        assert req.url.path == "/open-apis/contact/v3/users/ou_1"
        assert req.url.params["user_id_type"] == "open_id"
        user = {"open_id": "ou_1", "department_ids": ["od-a", "od-b"]} if req.headers.get("x-case") != "no-field" else {"open_id": "ou_1"}
        return httpx.Response(200, json={"code": 0, "data": {"user": user}})

    fs = make_client(tmp_path, handler)
    assert fs.user_department_ids("ou_1") == ["od-a", "od-b"]
    fs.http.headers["x-case"] = "no-field"
    with pytest.raises(FeishuError, match="contact:user.department:readonly"):
        fs.user_department_ids("ou_1")


def test_send_text(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        seen.append(req)
        if json.loads(req.content)["receive_id"] == "ou_out_of_range":
            return httpx.Response(400, json={"code": 230013, "msg": "Bot has NO availability to this user"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})

    fs = make_client(tmp_path, handler)
    fs.send_text("ou_1", "张三 申请查看「周报」")
    req = seen[0]
    assert req.url.path == "/open-apis/im/v1/messages" and req.url.params["receive_id_type"] == "open_id"
    assert req.headers["authorization"].startswith("Bearer ")
    body = json.loads(req.content)
    assert body["receive_id"] == "ou_1" and body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "张三 申请查看「周报」"}  # content 是 JSON 字符串，不是对象
    with pytest.raises(FeishuError, match="发送飞书消息失败：code=230013"):
        fs.send_text("ou_out_of_range", "x")


def test_admins_and_notify_from_env(tmp_path, monkeypatch):
    from htmlshare_server.config import Settings

    monkeypatch.setenv("HTMLSHARE_PUBLIC_URL", "http://share.example.com")
    monkeypatch.setenv("HTMLSHARE_DEV_FAKE_LOGIN", "1")
    monkeypatch.setenv("HTMLSHARE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HTMLSHARE_ADMINS", raising=False)
    monkeypatch.delenv("HTMLSHARE_NOTIFY", raising=False)
    s = Settings.from_env()
    assert s.admins == () and s.notify is False
    monkeypatch.setenv("HTMLSHARE_ADMINS", " ou_Abc, Zhang.San@Example.com；ou_abc\nli.si@example.com ")
    monkeypatch.setenv("HTMLSHARE_NOTIFY", "1")
    s = Settings.from_env()
    assert s.admins == ("ou_abc", "zhang.san@example.com", "li.si@example.com") and s.notify is True


def test_network_errors_become_feishu_errors(tmp_path):
    # 连不上、超时不是飞书返回的错误码，但调用方只认 FeishuError 来降级（沿用缓存、503、稍后重试），漏了就是 500
    def handler(req: httpx.Request):
        if req.url.path.endswith("/tenant_access_token/internal"):
            return _tenant_ok(req)
        if req.url.path.endswith("/im/v1/messages"):
            raise httpx.ReadTimeout("读超时", request=req)
        raise httpx.ConnectError("连不上", request=req)

    fs = make_client(tmp_path, handler)
    calls = [
        lambda: fs.login("code", "http://testserver/auth/callback"),
        lambda: fs.resolve(["a@corp.com"], []),
        fs.list_departments,
        lambda: fs.user_department_ids("ou_1"),
        lambda: fs.list_department_users("od-a"),
        lambda: fs.send_text("ou_1", "x"),
    ]
    for call in calls:
        with pytest.raises(FeishuError, match="连接飞书失败"):
            call()
