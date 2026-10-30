"""htmlshare 命令行。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser

from . import __version__, config
from .api import Client, HtmlShareError


def make_client(server: str | None = None) -> Client:
    return Client(server=server)


def _when(iso: str | None) -> str:
    return iso[:16].replace("T", " ") if iso else "永不过期"


def _viewers_text(page: dict) -> str:
    labels = [v["label"] for v in page.get("viewers", [])]
    return "、".join(labels) if labels else "仅自己"


def _print_page(page: dict, verb: str) -> None:
    print(f"{verb}：{page['url']}")
    print(f"可见人：{_viewers_text(page)}（发布者本人总是可见）")
    print(f"过期时间：{_when(page.get('expires_at'))}")
    unresolved = page.get("unresolved") or []
    if unresolved:
        print("注意：以下可见人没有加上：")
        for u in unresolved:
            print(f"  - {u['input']}：{u['reason']}")


def cmd_login(args) -> int:
    server = (args.server or config.load().get("server") or "").rstrip("/")
    if not server:
        print("请用 --server 指定分享站地址，例如：htmlshare login --server https://share.example.com", file=sys.stderr)
        return 2
    client = make_client(server)
    start = client.start_login()
    print(f"请在浏览器中用飞书登录并确认授权：\n  {start['verification_url']}")
    print(f"确认页上显示的代码应为：{start['user_code']}", flush=True)
    if not args.no_browser:
        webbrowser.open(start["verification_url"])
    deadline = time.time() + start.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(start.get("interval", 2))
        res = client.poll_login(start["device_code"])
        if res.get("status") == "ok":
            path = config.save(server=server, token=res["token"])
            print(f"登录成功：{res.get('user') or ''}，配置已保存到 {path}")
            return 0
        if res.get("status") == "expired":
            break
    print("登录超时或已失效，请重新运行 htmlshare login", file=sys.stderr)
    return 1


def cmd_logout(args) -> int:
    try:
        make_client().revoke()
    except HtmlShareError as e:
        print(f"服务端撤销 token 失败（{e}），仍会清除本地登录信息", file=sys.stderr)
    config.save(token=None)
    print("已退出登录")
    return 0


def cmd_whoami(args) -> int:
    me = make_client().me()
    print(f"{me.get('name') or me['open_id']} @ {me['server']}")
    return 0


def cmd_publish(args) -> int:
    page = make_client().publish(args.file, name=args.name, viewers=args.to, expires_days=args.expires)
    if args.json:
        print(json.dumps(page, ensure_ascii=False, indent=2))
    else:
        _print_page(page, "已发布" if page.get("created") else "已更新")
    return 0


def cmd_ls(args) -> int:
    pages = make_client().list_pages()
    if args.json:
        print(json.dumps(pages, ensure_ascii=False, indent=2))
        return 0
    if not pages:
        print("还没有发布过页面")
        return 0
    for p in pages:
        title = f"  {p['title']}" if p.get("title") else ""
        print(f"{p['name']}{title}")
        print(f"    {p['url']}")
        print(f"    可见人：{_viewers_text(p)}  访问：{p.get('views', 0)} 次  过期：{_when(p.get('expires_at'))}")
    return 0


def cmd_share(args) -> int:
    if args.set is None and not args.add and not args.remove and args.expires is None:
        page = make_client().get_page(args.name)
    else:
        page = make_client().update(
            args.name,
            set_viewers=args.set,
            add_viewers=args.add,
            remove_viewers=args.remove,
            expires_days=args.expires,
        )
    if args.json:
        print(json.dumps(page, ensure_ascii=False, indent=2))
    else:
        _print_page(page, "页面")
    return 0


def cmd_rm(args) -> int:
    make_client().delete(args.name)
    print(f"已删除：{args.name}")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    run()
    return 0


def _list_arg(value: str) -> list[str]:
    return [v for v in value.replace("，", ",").split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="htmlshare", description="把 HTML 发布到内网分享站（飞书登录 + 按人授权）")
    p.add_argument("--version", action="version", version=f"htmlshare {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="用飞书登录（打开浏览器确认）")
    sp.add_argument("--server", help="分享站地址，例如 https://share.example.com")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印链接")
    sp.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="退出登录并撤销本机 token").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="查看当前登录身份").set_defaults(func=cmd_whoami)

    sp = sub.add_parser("publish", help="发布 HTML 文件（同名页面会被覆盖）")
    sp.add_argument("file", help="HTML 文件路径")
    sp.add_argument("-n", "--name", help="页面名（URL 里的名字），默认取文件名；只能用小写字母、数字、-")
    sp.add_argument(
        "-t", "--to", type=_list_arg, help="可见人，逗号分隔：飞书邮箱、手机号，或 all（全员）。不填：新页面仅自己可见，已有页面保留原权限"
    )
    sp.add_argument("-e", "--expires", type=int, help="多少天后过期，0 表示永不过期；默认由服务端决定（通常 30 天）")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("ls", help="列出我发布的页面")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("share", help="查看或修改页面的可见人 / 过期时间")
    sp.add_argument("name")
    sp.add_argument("--set", type=_list_arg, help="把可见人整体替换为这些（传空字符串表示仅自己）")
    sp.add_argument("--add", type=_list_arg, help="追加可见人")
    sp.add_argument("--remove", type=_list_arg, help="移除可见人")
    sp.add_argument("-e", "--expires", type=int, help="从现在起多少天后过期，0 表示永不过期")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_share)

    sp = sub.add_parser("rm", help="删除页面")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_rm)

    sub.add_parser("mcp", help="以 MCP server（stdio）方式运行，给 AI 工具接入").set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "mcp":
        # 管道输出沿用系统编码（中文 Windows 是 GBK），只保证遇到编不了的字符时不崩
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass
    try:
        return args.func(args)
    except HtmlShareError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
