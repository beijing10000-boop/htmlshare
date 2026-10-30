"""客户端测试：直接对接服务端 app（进程内），不走网络。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import Env, login  # 服务端测试里的工具
from htmlshare import cli, config, mcp_server
from htmlshare.api import Client, HtmlShareError, slugify

TOKEN = "hs_test_ou_alice"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HTMLSHARE_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.delenv("HTMLSHARE_SERVER", raising=False)
    monkeypatch.delenv("HTMLSHARE_TOKEN", raising=False)
    env = Env(tmp_path / "srv")
    env.user_dir.sync()  # 测试里不跑后台线程，手动同步一次通讯录
    env.api("ou_alice")  # 生成 TOKEN
    http = TestClient(env.app)
    env.client = Client(server="http://testserver", token=TOKEN, http=http)
    monkeypatch.setattr(cli, "make_client", lambda server=None: env.client)
    monkeypatch.setattr(mcp_server, "make_client", lambda: env.client)
    return env


@pytest.fixture
def report(tmp_path):
    f = tmp_path / "Q3 Report.html"
    f.write_text("<html><head><title>三季度报告</title></head><body>ok</body></html>", encoding="utf-8")
    return f


def test_slugify():
    assert slugify("Q3 Report_final.html") == "q3-report-final"
    assert slugify("--a--b--.html") == "a-b"
    assert slugify("三季度报告.html").startswith("page-")
    assert len(slugify("x" * 100 + ".html")) == 64


def test_cli_publish_ls_share_rm(env, report, capsys):
    assert cli.main(["publish", str(report), "--to", "bob@example.com，nobody@example.com"]) == 0
    out = capsys.readouterr().out
    assert "已发布：http://testserver/q3-report.html" in out
    assert "bob@example.com" in out
    assert "nobody@example.com：飞书里查不到" in out

    assert cli.main(["ls"]) == 0
    assert "三季度报告" in capsys.readouterr().out

    assert cli.main(["share", "q3-report", "--add", "carol@example.com", "--remove", "bob@example.com", "--json"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert [v["label"] for v in page["viewers"]] == ["张三 <carol@example.com>"]  # 通讯录里有，带上姓名

    assert cli.main(["share", "q3-report", "--set", ""]) == 0
    assert "仅自己" in capsys.readouterr().out

    assert cli.main(["rm", "q3-report"]) == 0
    assert cli.main(["ls"]) == 0
    assert "还没有发布过页面" in capsys.readouterr().out


def test_cli_publish_to_department(env, report, capsys):
    args = ["publish", str(report), "--name", "ops", "--to", "dept:技术中心/运维部,dept:运维部,dept:Sales Team"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "可见人：部门:技术中心/运维部、部门:销售部/Sales Team" in out
    assert "dept:运维部：有 2 个同名部门" in out

    res = mcp_server.update_viewers("ops", remove=["dept:Sales Team"])
    assert res["viewers"] == ["部门:技术中心/运维部"]


def test_whois_and_publish_by_name(env, report, capsys):
    assert cli.main(["whois", "张三"]) == 0
    out = capsys.readouterr().out
    assert "张三 (Zhang San)  bob@example.com  ou_bob" in out
    assert "技术中心/运维部/一组" in out
    assert "重名时用邮箱或 open_id 指定可见人" in out

    assert cli.main(["publish", str(report), "--name", "byname", "--to", "戴维,张三"]) == 0
    out = capsys.readouterr().out
    assert "可见人：戴维 <dave@example.com>" in out
    assert "张三：有 2 个人叫“张三”" in out

    assert cli.main(["whois", "没有这个人"]) == 0
    assert "通讯录里没找到" in capsys.readouterr().out


def test_mcp_find_user(env):
    res = mcp_server.find_user("dave")
    assert [(u["name"], u["email"]) for u in res["users"]] == [("戴维", "dave@example.com")]
    assert res["users"][0]["departments"] == ["技术中心/研发部", "销售部/Sales Team"]


def test_cli_error_exit_code(env, tmp_path, report, capsys):
    assert cli.main(["publish", str(tmp_path / "missing.html")]) == 1
    assert "找不到文件" in capsys.readouterr().err
    assert cli.main(["publish", str(report), "--name", "Bad Name"]) == 1
    assert "页面名只能用小写字母" in capsys.readouterr().err


def test_cli_login_device_flow(env, monkeypatch, capsys):
    def approve(_seconds):
        with env.db.conn() as c:
            row = c.execute("SELECT user_code FROM cli_logins WHERE owner IS NULL").fetchone()
        if row:
            b = TestClient(env.app, follow_redirects=False)
            login(b, "alice@example.com")
            b.post("/cli/verify", data={"user_code": row["user_code"]})

    monkeypatch.setattr(cli.time, "sleep", approve)
    assert cli.main(["login", "--server", "http://testserver", "--no-browser"]) == 0
    assert "登录成功：alice" in capsys.readouterr().out
    saved = config.load()
    assert saved["server"] == "http://testserver"
    assert saved["token"].startswith("hs_")

    assert cli.main(["logout"]) == 0
    assert "token" not in config.load()


def test_client_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("HTMLSHARE_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.delenv("HTMLSHARE_SERVER", raising=False)
    monkeypatch.delenv("HTMLSHARE_TOKEN", raising=False)
    with pytest.raises(HtmlShareError, match="htmlshare login --server"):
        Client()
    with pytest.raises(HtmlShareError, match="还没登录"):
        Client(server="http://x").me()


def test_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HTMLSHARE_CONFIG", str(tmp_path / "cfg.json"))
    config.save(server="http://a", token="t1")
    monkeypatch.setenv("HTMLSHARE_TOKEN", "t2")
    assert config.load() == {"server": "http://a", "token": "t2"}


def test_mcp_tools(env, report):
    res = mcp_server.publish_html(file_path=str(report), name="rpt", viewers=["bob@example.com"])
    assert res["url"] == "http://testserver/rpt.html"
    assert res["viewers"] == ["张三 <bob@example.com>"]
    assert res["created"] is True and res["unresolved"] == []

    res = mcp_server.publish_html(html_content="<title>inline</title>hi", name="inline", viewers=["all"])
    assert res["viewers"] == ["全员"] and res["title"] == "inline"

    res = mcp_server.update_viewers("rpt", add=["carol@example.com"], remove=["bob@example.com"])
    assert res["viewers"] == ["张三 <carol@example.com>"]

    names = {p["name"] for p in mcp_server.list_my_pages()}
    assert names == {"rpt", "inline"}
    assert mcp_server.delete_page("inline")["ok"] is True
    assert mcp_server.whoami()["open_id"] == "ou_alice"


def test_mcp_errors_are_tool_errors(env):
    # 只有 ToolError 的消息能传到模型那边
    with pytest.raises(mcp_server.ToolError, match="绝对路径"):
        mcp_server.publish_html(file_path="report.html")
    with pytest.raises(mcp_server.ToolError, match="页面名只能用小写字母"):
        mcp_server.publish_html(html_content="<p>x</p>", name="Bad Name")
    with pytest.raises(mcp_server.ToolError, match="不存在"):
        mcp_server.delete_page("nope")
