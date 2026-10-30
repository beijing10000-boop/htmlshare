from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from htmlshare_server.app import create_app, hash_secret
from htmlshare_server.config import Settings
from htmlshare_server.feishu import Dept, FakeFeishu

DIRECTORY = {
    "alice@example.com": "ou_alice",
    "bob@example.com": "ou_bob",
    "carol@example.com": "ou_carol",
    "13800000000": "ou_dave",
    "dave@example.com": "ou_dave",
}

# 技术中心/运维部/一组、技术中心/研发部、销售部/运维部（与技术中心/运维部同名）、销售部/Sales Team
DEPARTMENTS = [
    Dept("od-tech", "技术中心", "0"),
    Dept("od-ops", "运维部", "od-tech"),
    Dept("od-ops-1", "一组", "od-ops"),
    Dept("od-rd", "研发部", "od-tech"),
    Dept("od-sales", "销售部", "0"),
    Dept("od-sales-ops", "运维部", "od-sales"),
    Dept("od-sales-team", "Sales Team", "od-sales"),
]
MEMBERSHIPS = {
    "ou_bob": ["od-ops-1"],
    "ou_carol": ["od-sales"],
    "ou_dave": ["od-rd", "od-sales-team"],
}


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        public_url="http://testserver",
        data_dir=tmp_path,
        secret_key="x" * 40,
        dev_fake_login=True,
        client_dist=tmp_path / "client-dist",
    )
    base.update(kw)
    return Settings(**base)


class Env:
    def __init__(self, tmp_path: Path, feishu=None, **kw):
        self.settings = make_settings(tmp_path, **kw)
        self.feishu = feishu or FakeFeishu(DIRECTORY, DEPARTMENTS, MEMBERSHIPS)
        self.app = create_app(self.settings, feishu=self.feishu)
        self.db = self.app.state.db

    def browser(self, email: str | None = None) -> TestClient:
        c = TestClient(self.app, follow_redirects=False)
        if email:
            login(c, email)
        return c

    def api(self, open_id: str) -> TestClient:
        token = "hs_test_" + open_id
        if not self.db.token_owner(hash_secret(token)):
            self.db.add_token(hash_secret(token), open_id, "test")
        return TestClient(self.app, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)


def login(c: TestClient, email: str, name: str = "", next_path: str = "/") -> None:
    r = c.get("/auth/login", params={"next": next_path})
    assert r.status_code == 302
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/auth/callback", params={"state": state, "email": email, "name": name or email.split("@")[0]})
    assert r.status_code == 302, r.text


def publish(api: TestClient, name: str, html: str = "<html><title>T</title><body>hi</body></html>", **form):
    data = {"name": name, **{k: str(v) for k, v in form.items()}}
    return api.post("/api/pages", data=data, files={"file": ("x.html", html.encode(), "text/html")})


def title_of(r) -> str:
    m = re.search(r"<title>(.*?)</title>", r.text)
    return m.group(1) if m else ""


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
