from __future__ import annotations

import pytest

from conftest import DEPARTMENTS, DIRECTORY, MEMBERSHIPS, Env, login, publish
from htmlshare_server.acl import split_viewers
from htmlshare_server.depts import DeptDirectory
from htmlshare_server.feishu import Dept, FakeFeishu, FeishuError


@pytest.fixture
def depts():
    d = DeptDirectory(FakeFeishu(departments=DEPARTMENTS))
    d.refresh()
    return d


def test_find(depts):
    assert [d.id for d in depts.find("研发部")] == ["od-rd"]
    assert [d.id for d in depts.find("运维部")] == ["od-ops", "od-sales-ops"]
    assert [d.id for d in depts.find("技术中心/运维部")] == ["od-ops"]
    assert [d.id for d in depts.find("/销售部/运维部/")] == ["od-sales-ops"]
    assert [d.id for d in depts.find("sales team")] == ["od-sales-team"]
    assert [d.id for d in depts.find("od-rd")] == ["od-rd"]
    assert depts.find("不存在") == []
    assert depts.find("研发中心/研发部") == []


def test_path_and_chain(depts):
    assert depts.path("od-ops-1") == "技术中心/运维部/一组"
    assert depts.chain("od-ops-1") == ["od-ops-1", "od-ops", "od-tech"]
    assert depts.chain("od-unknown") == ["od-unknown"]


def test_chain_survives_cycle():
    d = DeptDirectory(FakeFeishu(departments=[Dept("a", "A", "b"), Dept("b", "B", "a")]))
    d.refresh()
    assert d.chain("a") == ["a", "b"]


def test_refresh_ttl_and_stale_fallback():
    fs = FakeFeishu(departments=DEPARTMENTS)
    d = DeptDirectory(fs, ttl=600)
    d.refresh()
    d.refresh()
    assert fs.calls["list_departments"] == 1

    def boom():
        raise FeishuError("down")

    fs.list_departments = boom
    d.refresh(force=True)  # 有旧数据：不抛，继续用旧的
    assert "od-ops" in d

    with pytest.raises(FeishuError):
        DeptDirectory(fs).refresh()  # 没有旧数据：抛出


def test_split_keeps_dept_names_with_spaces():
    assert split_viewers("dept:Sales Team, bob@example.com  carol@example.com；部门：技术中心/运维部") == [
        "dept:Sales Team",
        "bob@example.com",
        "carol@example.com",
        "部门：技术中心/运维部",
    ]


def test_publish_to_dept_includes_sub_departments(env):
    r = publish(env.api("ou_alice"), "ops", viewers="dept:技术中心/运维部")
    body = r.json()
    assert body["viewers"] == [{"kind": "dept", "value": "od-ops", "label": "部门:技术中心/运维部"}]
    assert body["unresolved"] == []
    assert env.browser("bob@example.com").get("/ops.html").status_code == 200  # 一组属于运维部
    assert env.browser("carol@example.com").get("/ops.html").status_code == 403
    assert env.browser("erin@example.com").get("/ops.html").status_code == 403  # 不在任何部门


def test_dept_with_space_and_multi_dept_user(env):
    r = publish(env.api("ou_alice"), "sales", viewers="dept:Sales Team")
    assert [v["value"] for v in r.json()["viewers"]] == ["od-sales-team"]
    assert env.browser("dave@example.com").get("/sales.html").status_code == 200  # dave 同时在研发部和 Sales Team


def test_ambiguous_and_unknown_dept(env):
    body = publish(env.api("ou_alice"), "x", viewers="dept:运维部,部门:财务部").json()
    assert body["viewers"] == []
    reasons = {u["input"]: u["reason"] for u in body["unresolved"]}
    assert "有 2 个同名部门：技术中心/运维部、销售部/运维部" in reasons["dept:运维部"]
    assert "找不到这个部门" in reasons["部门:财务部"]


def test_remove_dept_viewer(env):
    alice = env.api("ou_alice")
    publish(alice, "doc", viewers="dept:技术中心/运维部,dept:研发部,bob@example.com")
    r = alice.patch("/api/pages/doc", json={"remove_viewers": ["dept:运维部"]})
    assert sorted(v["value"] for v in r.json()["viewers"]) == ["od-rd", "ou_bob"]
    r = alice.patch("/api/pages/doc", json={"remove_viewers": ["部门：技术中心/研发部"]})
    assert [v["value"] for v in r.json()["viewers"]] == ["ou_bob"]


def test_user_dept_cache_and_relogin(env):
    publish(env.api("ou_alice"), "ops", viewers="dept:研发部")
    bob = env.browser("bob@example.com")
    assert bob.get("/ops.html").status_code == 403
    assert bob.get("/ops.html").status_code == 403
    assert env.feishu.calls["user_department_ids"] == 1  # 第二次走缓存

    env.feishu.memberships = dict(MEMBERSHIPS, ou_bob=["od-rd"])  # 调岗到研发部
    assert bob.get("/ops.html").status_code == 403  # 缓存未过期，仍是旧部门
    login(bob, "bob@example.com")  # 重新登录清缓存
    assert bob.get("/ops.html").status_code == 200
    assert env.feishu.calls["user_department_ids"] == 2


def test_pages_without_dept_acl_never_query_feishu(env):
    publish(env.api("ou_alice"), "p", viewers="bob@example.com")
    assert env.browser("carol@example.com").get("/p.html").status_code == 403
    assert env.feishu.calls["user_department_ids"] == 0


def test_feishu_error_on_user_depts_gives_503(tmp_path):
    class Broken(FakeFeishu):
        def user_department_ids(self, open_id):
            raise FeishuError("飞书挂了")

    env = Env(tmp_path, feishu=Broken(DIRECTORY, DEPARTMENTS, MEMBERSHIPS))
    publish(env.api("ou_alice"), "ops", viewers="dept:研发部")
    r = env.browser("bob@example.com").get("/ops.html")
    assert r.status_code == 503
    assert "暂时无法确认权限" in r.text
