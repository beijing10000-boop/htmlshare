from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from conftest import DEPARTMENTS, PEOPLE, Env, publish
from htmlshare_server.acl import split_viewers
from htmlshare_server.depts import DeptDirectory
from htmlshare_server.directory import UserDirectory
from htmlshare_server.feishu import FakeFeishu, FeishuError


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.user_dir.sync()
    return e


def test_sync_merges_multi_department_users(env):
    assert env.user_dir.ready()
    dave = env.user_dir.get("ou_dave")
    assert dave.dept_ids == ("od-rd", "od-sales-team")
    assert env.user_dir.dept_paths(dave) == ["技术中心/研发部", "销售部/Sales Team"]
    assert len(env.user_dir.search("example.com")) == 4  # erin 没邮箱
    assert env.feishu.calls["list_department_users"] == 1 + len(DEPARTMENTS)  # 根 + 每个部门一次


def test_lookup_is_exact_and_ignores_space_and_case(env):
    assert sorted(u.open_id for u in env.user_dir.lookup("张三")) == ["ou_bob", "ou_carol"]
    assert len(env.user_dir.lookup("zhang san")) == 2
    assert len(env.user_dir.lookup("ZhangSan")) == 2
    assert [u.open_id for u in env.user_dir.lookup("dave smith")] == ["ou_dave"]
    assert env.user_dir.lookup("张") == []  # 姓名授权只认全名
    assert [u.open_id for u in env.user_dir.search("张")][:2] == ["ou_bob", "ou_carol"]  # 搜索可以模糊


def test_split_keeps_english_names():
    assert split_viewers("John Smith, a@x.com b@x.com，name:Dave Smith、 13800000000") == [
        "John Smith",
        "a@x.com",
        "b@x.com",
        "name:Dave Smith",
        "13800000000",
    ]


def test_publish_by_name(env):
    body = publish(env.api("ou_alice"), "doc", viewers="戴维, name:艾琳, Alice Wang").json()
    assert {(v["value"], v["label"]) for v in body["viewers"]} == {
        ("ou_dave", "戴维 <dave@example.com>"),
        ("ou_erin", "艾琳"),
        ("ou_alice", "爱丽丝 <alice@example.com>"),
    }
    assert body["unresolved"] == []
    assert env.browser("dave@example.com").get("/doc.html").status_code == 200


def test_duplicate_name_lists_candidates(env):
    body = publish(env.api("ou_alice"), "doc", viewers="张三").json()
    assert body["viewers"] == []
    reason = body["unresolved"][0]["reason"]
    assert reason.startswith("有 2 个人叫“张三”")
    assert "张三（技术中心/运维部/一组，bob@example.com，ou_bob）" in reason
    assert "张三（销售部，carol@example.com，ou_carol）" in reason
    # 按提示改用邮箱即可
    body = publish(env.api("ou_alice"), "doc", viewers="carol@example.com").json()
    assert [v["label"] for v in body["viewers"]] == ["张三 <carol@example.com>"]


def test_unknown_name_triggers_early_resync(env):
    env.user_dir._last_attempt = 0  # 假装很久没同步
    body = publish(env.api("ou_alice"), "doc", viewers="不存在的人").json()
    assert "通讯录里没有叫“不存在的人”的人" in body["unresolved"][0]["reason"]
    assert env.user_dir._wake.is_set()


def test_remove_by_name(env):
    alice = env.api("ou_alice")
    publish(alice, "doc", viewers="戴维,bob@example.com,carol@example.com")
    r = alice.patch("/api/pages/doc", json={"remove_viewers": ["Dave Smith"]})
    assert sorted(v["value"] for v in r.json()["viewers"]) == ["ou_bob", "ou_carol"]
    r = alice.patch("/api/pages/doc", json={"remove_viewers": ["张三"]})  # 两个张三都移除
    assert r.json()["viewers"] == []


def test_search_api(env):
    alice = env.api("ou_alice")
    r = alice.get("/api/users/search", params={"q": "zhang"})
    users = r.json()["users"]
    assert [u["email"] for u in users] == ["bob@example.com", "carol@example.com"]
    assert users[0]["departments"] == ["技术中心/运维部/一组"]
    assert set(users[0]) == {"open_id", "name", "en_name", "email", "departments"}  # 不返回手机号等
    assert env.browser().get("/api/users/search", params={"q": "zhang"}).status_code == 401
    assert alice.get("/api/users/search", params={"q": " "}).status_code == 400


def test_search_before_first_sync_is_503(tmp_path):
    env = Env(tmp_path)
    r = env.api("ou_alice").get("/api/users/search", params={"q": "张三"})
    assert r.status_code == 503 and "通讯录还在同步" in r.json()["detail"]


def test_failed_sync_keeps_old_snapshot(env):
    def broken(dept_id):
        if dept_id == "od-sales":
            raise FeishuError("限流")
        return FakeFeishu(people=PEOPLE).list_department_users(dept_id)

    env.feishu.list_department_users = broken
    env.user_dir.retries = 1
    before = env.user_dir.synced_at()
    with pytest.raises(FeishuError):
        env.user_dir.sync()
    assert env.user_dir.synced_at() == before
    assert len(env.user_dir.lookup("张三")) == 2  # 旧快照还在，没有被残缺数据覆盖


def test_kick_respects_min_gap(tmp_path):
    fs = FakeFeishu(departments=DEPARTMENTS, people=PEOPLE)
    from htmlshare_server.db import Database

    d = UserDirectory(fs, DeptDirectory(fs), Database(tmp_path / "x.db"), min_kick_gap=1800)
    d.kick()
    assert d._wake.is_set()
    d._wake.clear()
    d.kick()
    assert not d._wake.is_set()


def test_lifespan_starts_background_sync(tmp_path):
    env = Env(tmp_path)
    assert not env.user_dir.ready()
    with TestClient(env.app):
        deadline = time.time() + 5
        while not env.user_dir.ready() and time.time() < deadline:
            time.sleep(0.05)
    assert env.user_dir.ready()


def test_lifespan_respects_switch(tmp_path):
    env = Env(tmp_path, directory_sync=False)
    with TestClient(env.app):
        time.sleep(0.3)
    assert not env.user_dir.ready()
