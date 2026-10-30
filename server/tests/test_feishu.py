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
