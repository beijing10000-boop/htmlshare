"""网页上的页面设置：改可见人（含重名候选一键选）、全员 / 仅自己、过期时间、删除、输入联想。"""

from __future__ import annotations

import pytest

from conftest import Env, publish

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.user_dir.sync()
    publish(e.api("ou_alice"), "doc", html="<title>周报</title>", viewers="dave@example.com")
    return e


def acl_values(env, name="doc"):
    return sorted(e.value for e in env.db.get_acl(name))


def post(c, action, **form):
    return c.post("/me/pages/doc", data={"action": action, **form}, headers=ORIGIN)


def test_settings_page_shows_viewers_and_link_from_home(env):
    alice = env.browser("alice@example.com")
    assert 'href="/me/pages/doc"' in alice.get("/").text
    r = alice.get("/me/pages/doc")
    assert r.status_code == 200
    assert "周报" in r.text and "戴维 &lt;dave@example.com&gt;" in r.text
    assert "设为全员可见" in r.text and "改为仅自己可见" in r.text


def test_only_owner_can_open_or_change(env):
    bob = env.browser("bob@example.com")
    assert bob.get("/me/pages/doc").status_code == 403
    assert post(bob, "set_private").status_code == 403
    assert acl_values(env) == ["ou_dave"]
    assert env.browser().get("/me/pages/doc").status_code == 302  # 未登录跳登录
    assert env.browser("alice@example.com").get("/me/pages/nope").status_code == 404


def test_add_remove_all_private(env):
    alice = env.browser("alice@example.com")
    r = post(alice, "add", viewers="dept:研发部, carol@example.com")
    assert "已更新可见人" in r.text
    assert acl_values(env) == ["od-rd", "ou_carol", "ou_dave"]

    post(alice, "remove", value="od-rd")
    assert acl_values(env) == ["ou_carol", "ou_dave"]

    r = post(alice, "add", viewers="all")
    assert acl_values(env) == ["*", "ou_carol", "ou_dave"]
    assert "设为全员可见" not in r.text  # 已经全员可见，不再显示这个按钮
    post(alice, "remove", value="*")  # 去掉全员，之前的个人还在
    assert acl_values(env) == ["ou_carol", "ou_dave"]

    r = post(alice, "set_private")
    assert acl_values(env) == [] and "仅自己可见" in r.text


def test_duplicate_name_offers_candidates_and_picking_one_works(env):
    alice = env.browser("alice@example.com")
    r = post(alice, "add", viewers="张三")
    assert "有 2 个人叫“张三”" in r.text
    assert 'name="viewers" value="ou_bob"' in r.text and 'name="viewers" value="ou_carol"' in r.text
    assert acl_values(env) == ["ou_dave"]
    post(alice, "add", viewers="ou_carol")  # 点「加上 张三（销售部…）」
    assert acl_values(env) == ["ou_carol", "ou_dave"]


def test_duplicate_dept_offers_candidates(env):
    r = post(env.browser("alice@example.com"), "add", viewers="dept:运维部")
    assert 'value="dept:od-ops"' in r.text and 'value="dept:od-sales-ops"' in r.text


def test_expiry_and_delete(env):
    alice = env.browser("alice@example.com")
    r = post(alice, "expiry", days="0")
    assert "已修改过期时间" in r.text and env.db.get_page("doc").expires_at is None
    r = post(alice, "expiry", days="9999")
    assert "过期天数必须在" in r.text
    r = post(alice, "delete")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert env.db.get_page("doc") is None


def test_foreign_origin_rejected(env):
    alice = env.browser("alice@example.com")
    r = alice.post("/me/pages/doc", data={"action": "set_private"}, headers={"Origin": "https://evil.com"})
    assert r.status_code == 403
    assert acl_values(env) == ["ou_dave"]


def test_suggest(env):
    alice = env.browser("alice@example.com")
    items = alice.get("/me/suggest", params={"q": "张"}).json()["items"]
    assert {"value": "bob@example.com", "label": "张三（技术中心/运维部/一组） bob@example.com"} in items
    items = alice.get("/me/suggest", params={"q": "运维"}).json()["items"]
    values = [i["value"] for i in items]
    # 同名部门用完整路径区分（路径本身唯一），比部门 id 好认
    assert "dept:技术中心/运维部" in values and "dept:销售部/运维部" in values
    assert "dept:技术中心/运维部/一组" in values
    assert any(i["value"] == "all" for i in alice.get("/me/suggest", params={"q": "全"}).json()["items"])
    assert env.browser().get("/me/suggest", params={"q": "张"}).status_code == 401


def test_api_unresolved_has_candidates(env):
    r = env.api("ou_alice").patch("/api/pages/doc", json={"add_viewers": ["张三"]})
    cands = r.json()["unresolved"][0]["candidates"]
    assert cands == [
        {"value": "ou_bob", "label": "张三（技术中心/运维部/一组，bob@example.com）"},  # 给人看的不带 open_id
        {"value": "ou_carol", "label": "张三（销售部，carol@example.com）"},
    ]


def test_dept_paths_load_tree_on_cold_start(tmp_path):
    # 服务刚启动、部门树还没加载时，第一个操作就是查人：部门要显示路径而不是 id
    env = Env(tmp_path)
    env.user_dir.sync()
    env.user_dir.depts._by_id.clear()
    env.user_dir.depts._loaded_at = 0
    items = env.browser("alice@example.com").get("/me/suggest", params={"q": "戴维"}).json()["items"]
    assert items[0]["label"] == "戴维（技术中心/研发部） dave@example.com"
