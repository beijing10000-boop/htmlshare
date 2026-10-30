"""首页「分享给我的页面」：列出来的必须都打得开，打得开的（别人发布的）必须都列出来。"""

from __future__ import annotations

from conftest import DEPARTMENTS, DIRECTORY, MEMBERSHIPS, PEOPLE, Env, publish
from htmlshare_server.feishu import FakeFeishu, FeishuError


def setup_pages(env: Env) -> None:
    alice, carol, bob = env.api("ou_alice"), env.api("ou_carol"), env.api("ou_bob")
    publish(alice, "to-bob", html="<title>给 bob</title>", viewers="bob@example.com")
    publish(alice, "all-page", html="<title>全员</title>", viewers="all")
    publish(alice, "dept-page", html="<title>运维部</title>", viewers="dept:技术中心/运维部")  # bob 在下级「一组」
    publish(alice, "both", html="<title>两条都命中</title>", viewers="all,bob@example.com")
    publish(alice, "private", html="<title>私有页面甲</title>")
    publish(alice, "to-carol", html="<title>给 carol</title>", viewers="carol@example.com")
    publish(alice, "old", html="<title>早就过期的页面乙</title>", viewers="bob@example.com")
    with env.db.conn() as c:
        c.execute("UPDATE pages SET expires_at=1 WHERE name='old'")
    publish(carol, "carol-all", html="<title>carol 的</title>", viewers="all")
    publish(bob, "bob-own", html="<title>bob 自己的</title>", viewers="all")


def test_shared_list_matches_view_permission(env):
    env.user_dir.sync()  # 发布者没在浏览器登录过，名字来自通讯录
    setup_pages(env)
    shared = env.app.state.service.shared_with("ou_bob")
    assert shared["dept_error"] is None
    via = {p["name"]: p["via"] for p in shared["pages"]}
    assert via == {
        "to-bob": "直接分享给你",
        "all-page": "全员可见",
        "dept-page": "部门:技术中心/运维部",
        "both": "直接分享给你",  # 多条命中时取最具体的
        "carol-all": "全员可见",
    }
    owners = {p["name"]: p["owner_name"] for p in shared["pages"]}
    assert owners["carol-all"] == "张三" and owners["to-bob"] == "爱丽丝"

    bob = env.browser("bob@example.com")
    for name in ["to-bob", "all-page", "dept-page", "both", "carol-all", "private", "to-carol", "old"]:
        listed = name in via
        status = bob.get(f"/{name}.html").status_code
        assert (status == 200) == listed, (name, status)


def test_home_shows_mine_and_shared(env):
    setup_pages(env)
    r = env.browser("bob@example.com").get("/")
    mine, rest = r.text.split("我发布的页面")[1].split("分享给我的页面")
    shared = rest.split("我的接入凭证")[0]
    assert "bob-own" in mine and "carol-all" not in mine
    assert "给 bob" in shared and "carol 的" in shared and "部门:技术中心/运维部" in shared
    assert "bob-own" not in shared
    assert "私有页面甲" not in shared and "早就过期的页面乙" not in shared and "给 carol" not in shared


def test_empty_shared_list(env):
    r = env.browser("erin@example.com").get("/")
    assert "还没有人分享页面给你" in r.text and "还没有发布过页面" in r.text


def test_no_dept_acl_means_no_feishu_call(env):
    publish(env.api("ou_alice"), "p", viewers="bob@example.com")
    env.browser("bob@example.com").get("/")
    assert env.feishu.calls["user_department_ids"] == 0


def test_feishu_down_still_lists_the_rest(tmp_path):
    class Broken(FakeFeishu):
        def user_department_ids(self, open_id):
            raise FeishuError("飞书挂了")

    env = Env(tmp_path, feishu=Broken(DIRECTORY, DEPARTMENTS, MEMBERSHIPS, PEOPLE))
    alice = env.api("ou_alice")
    publish(alice, "dept-page", viewers="dept:研发部")
    publish(alice, "to-bob", viewers="bob@example.com")
    shared = env.app.state.service.shared_with("ou_bob")
    assert [p["name"] for p in shared["pages"]] == ["to-bob"]
    assert "按部门授权给你的页面暂时没有列出来" in shared["dept_error"]
    assert "按部门授权给你的页面暂时没有列出来" in env.browser("bob@example.com").get("/").text


def test_shared_api(env):
    setup_pages(env)
    r = env.api("ou_bob").get("/api/shared-pages")
    assert r.status_code == 200
    assert {p["name"] for p in r.json()["pages"]} == {"to-bob", "all-page", "dept-page", "both", "carol-all"}
    assert env.browser().get("/api/shared-pages").status_code == 401
