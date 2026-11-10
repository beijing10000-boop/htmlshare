"""网页控制台的路由（浏览器 Cookie 会话）：概览、我发布的、分享给我的、访问申请、接入 AI 工具、操作审计、管理员页面。

改数据的请求都是表单 POST：校验登录和来源（Origin），成功后跳回 GET 页面并带 ?done=xxx 显示提示，
这样刷新、后退都不会重复提交。规则和 REST、远程 MCP 走同一个 PageService。
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import web
from .config import Settings
from .db import Database
from .directory import UserDirectory
from .remote_mcp import connect_prompt
from .service import AUDIT_GROUPS, PageService, name_from_filename, normalize_name

AUDIT_PAGE = 100  # 操作审计每页条数

# 操作成功后跳回页面时的提示：?done=<键> -> (类型, 文字)
FLASH = {
    "published": ("success", "已发布"),
    "updated": ("success", "内容已更新"),
    "viewers": ("success", "已更新可见人"),
    "removed": ("success", "已移除"),
    "private": ("success", "已改为仅自己可见"),
    "expiry": ("success", "已修改过期时间"),
    "deleted": ("success", "页面已删除"),
    "approved": ("success", "已通过，对方现在可以打开这个页面了"),
    "rejected": ("info", "已拒绝这个申请"),
    "handled": ("info", "这个申请已经处理过了"),
    "revoked": ("success", "凭证已撤销"),
    "taken_down": ("success", "页面已下线"),
    "sync": ("success", "已通知后台同步通讯录，通常一分钟内完成，稍后刷新查看"),
}


@dataclass
class Viewer:
    open_id: str
    name: str
    email: str

    @property
    def label(self) -> str:
        return self.name or self.email or self.open_id


@dataclass
class Deps:
    """app.py 里建好的东西，控制台路由要用的。"""

    s: Settings
    db: Database
    service: PageService
    user_dir: UserDirectory
    read_session: Callable[[Request], Viewer | None]
    login_redirect: Callable[[Request], RedirectResponse]
    origin_ok: Callable[[Request], bool]
    new_token: Callable[[str, str], str]
    client_wheel: Callable[[], Path | None]


def safe_next(target: str | None) -> str:
    """只允许跳回站内路径。"""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(ch) < 0x20 for ch in target):
        return "/"
    return target


def register(app: FastAPI, d: Deps) -> None:
    s, db, service = d.s, d.db, d.service

    # ---------- helpers ----------
    def is_admin(user: Viewer) -> bool:
        if not s.admins:
            return False
        mine = {user.open_id.lower(), user.email.lower()}
        if entry := d.user_dir.get(user.open_id):
            mine.add(entry.email.lower())
        return bool(mine.intersection(s.admins) - {""})

    def shell(user: Viewer, active: str) -> web.Shell:
        return web.Shell(user.label, is_admin(user), db.pending_request_count(user.open_id), active)

    def flash(request: Request) -> tuple[str, str] | None:
        return FLASH.get(request.query_params.get("done", ""))

    def expiry_choices() -> list[int]:
        return [n for n in (1, 7, 30, 90, 180, 365) if n <= s.max_expire_days]

    def upload_context() -> dict:
        return {"max_mb": s.max_upload_mb, "default_days": s.default_expire_days, "expiry_choices": expiry_choices()}

    def done(target: str, code: str) -> RedirectResponse:
        path, _, fragment = safe_next(target).partition("#")
        url = f"{path}{'&' if '?' in path else '?'}done={code}" + (f"#{fragment}" if fragment else "")
        return RedirectResponse(url, status_code=303)

    def notice(user: Viewer, title: str, text: str, status: int, back: tuple[str, str], active: str = "") -> Response:
        """控制台里的整页提示：说明哪里不行，给一个去处。"""
        return web.render("notice.html", status, shell=shell(user, active), title=title, text=text, back=back)

    def poster(request: Request) -> tuple[Viewer | None, Response | None]:
        """表单 POST 的统一检查。会话过期时给提示页而不是直接跳飞书（表单提交后跨站跳转会被浏览器拦）。"""
        user = d.read_session(request)
        if not user:
            return None, web.message("登录已过期", "请重新登录后再操作。", 401, link=("使用飞书账号登录", "/auth/login"))
        if not d.origin_ok(request):
            return None, web.message("请求被拒绝", "来源校验失败。", 403, user.label)
        return user, None

    def admin_only(user: Viewer, active: str) -> Response | None:
        if is_admin(user):
            return None
        return notice(
            user, "需要管理员权限", "这个页面只有管理员能打开。管理员由部署时的环境变量 HTMLSHARE_ADMINS 指定。", 403, ("回到概览", "/"), active
        )

    def page_view(user: Viewer, name: str, request: Request | None = None, status: int = 200, **extra) -> Response:
        try:
            page = service.page_detail(user.open_id, name)
        except HTTPException as e:
            return notice(user, "无法设置这个页面", str(e.detail), e.status_code, ("回到我发布的", "/me/pages"), "pages")
        context = {"notice": None, "unresolved": [], "error": None, **extra, **upload_context()}
        return web.render("page.html", status, shell=shell(user, "pages"), flash=flash(request) if request else None, p=page, **context)

    # ---------- 工作台 ----------
    @app.get("/")
    def overview(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        return web.render(
            "overview.html",
            shell=shell(user, "overview"),
            flash=flash(request),
            o=service.overview(user.open_id),
            renew_days=min(30, s.max_expire_days),
        )

    @app.get("/me/pages")
    def my_pages(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        pages = service.console_pages(user.open_id)
        return web.render("pages.html", shell=shell(user, "pages"), flash=flash(request), pages=pages, **upload_context())

    @app.post("/me/pages")
    def upload_page(
        request: Request,
        file: UploadFile = File(...),
        name: str = Form(""),
        viewers: str = Form(""),
        days: str = Form(""),
    ):
        """网页上传 HTML（对话框里用 fetch 提交）：返回 JSON，出错时对话框原地显示原因。"""
        user = d.read_session(request)
        if not user:
            return JSONResponse({"ok": False, "error": "登录已过期，请刷新页面重新登录后再上传"}, status_code=401)
        if not d.origin_ok(request):
            return JSONResponse({"ok": False, "error": "来源校验失败"}, status_code=403)
        try:
            page_name = normalize_name(name) if name.strip() else name_from_filename(file.filename or "")
            if days.strip() and not days.strip().isdigit():
                raise HTTPException(400, "过期天数要填数字")
            page = service.publish(
                user.open_id,
                page_name,
                file.file.read(service.max_bytes + 1),
                viewers if viewers.strip() else None,
                int(days) if days.strip() else None,
                via="web",
            )
        except HTTPException as e:
            return JSONResponse({"ok": False, "error": str(e.detail)}, status_code=e.status_code)
        return {
            "ok": True,
            "name": page["name"],
            "url": page["url"],
            "created": page["created"],
            "unresolved": [{"input": u["input"], "reason": u["reason"]} for u in page["unresolved"]],
            "redirect": f"/me/pages/{page['name']}?done={'published' if page['created'] else 'updated'}",
        }

    @app.get("/me/pages/{name}")
    def page_settings(name: str, request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        return page_view(user, name, request)

    @app.post("/me/pages/{name}")
    def page_action(
        name: str,
        request: Request,
        action: str = Form(...),
        viewers: str = Form(""),
        value: str = Form(""),
        days: int | None = Form(None),
        next: str = Form(""),
    ):
        """改可见人 / 过期时间 / 删除；只有发布者本人能改，规则和 API、MCP 完全一样。"""
        user, denied = poster(request)
        if denied:
            return denied
        owner = user.open_id
        back = next or f"/me/pages/{quote(name)}"
        try:
            if action == "add":
                res = service.update(owner, name, add_viewers=[viewers], via="web")
                if res["unresolved"]:  # 重名、同名部门、查不到：留在页面上给候选，不跳转
                    return page_view(user, name, unresolved=res["unresolved"])
                return done(back, "viewers")
            if action == "remove":
                service.update(owner, name, remove_viewers_=[value], via="web")
                return done(back, "removed")
            if action == "set_private":
                service.update(owner, name, set_viewers=[], via="web")
                return done(back, "private")
            if action == "expiry":
                service.update(owner, name, expires_days=days if days is not None else -1, via="web")
                return done(back, "expiry")
            if action == "delete":
                service.delete(owner, name, via="web")
                return done("/me/pages", "deleted")
            error = "未知操作"
        except HTTPException as e:
            if e.status_code in (403, 404):
                return notice(user, "无法设置这个页面", str(e.detail), e.status_code, ("回到我发布的", "/me/pages"), "pages")
            error = str(e.detail)
        return page_view(user, name, status=400, error=error)

    @app.get("/shared")
    def shared_pages(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        shared = service.shared_with(user.open_id)
        return web.render("shared.html", shell=shell(user, "shared"), pages=shared["pages"], dept_error=shared["dept_error"])

    @app.get("/me/suggest")
    def suggest(request: Request, q: str = ""):
        if not d.read_session(request):
            raise HTTPException(401, "请先登录")
        return service.suggest(q)

    # ---------- 访问申请 ----------
    @app.get("/requests")
    def access_requests(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        return web.render(
            "requests.html",
            shell=shell(user, "requests"),
            flash=flash(request),
            pending=service.owner_requests(user.open_id, pending=True),
            done=service.owner_requests(user.open_id, pending=False),
            mine=service.my_requests(user.open_id),
        )

    @app.post("/requests/{request_id}")
    def decide_request(request_id: int, request: Request, action: str = Form(...), next: str = Form("")):
        user, denied = poster(request)
        if denied:
            return denied
        back = next or "/requests"
        if action not in ("approve", "reject"):
            return notice(user, "无法处理这个申请", "未知操作。", 400, ("回到访问申请", "/requests"), "requests")
        try:
            service.decide_request(user.open_id, request_id, action == "approve")
        except HTTPException as e:
            if e.status_code == 409:
                return done(back, "handled")
            return notice(user, "无法处理这个申请", str(e.detail), e.status_code, ("回到访问申请", "/requests"), "requests")
        return done(back, "approved" if action == "approve" else "rejected")

    @app.post("/me/requests")
    def request_access(request: Request, page: str = Form(...), reason: str = Form("")):
        """「没有访问权限」页面上的申请按钮。提交后回到那个页面：还没通过就显示「已申请」，已经能看就直接打开。"""
        user, denied = poster(request)
        if denied:
            return denied
        try:
            name = normalize_name(page)
            service.request_access(user.open_id, name, reason)
        except HTTPException as e:
            return web.message("无法提交申请", html.escape(str(e.detail)), e.status_code, user.label)
        return RedirectResponse(f"/{name}.html", status_code=303)

    # ---------- 接入 ----------
    @app.get("/connect")
    def connect(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        wheel = d.client_wheel()
        return web.render(
            "connect.html",
            shell=shell(user, "connect"),
            flash=flash(request),
            tokens=[dict(t) for t in db.list_tokens(user.open_id)],
            install_cmd=f"pip install {s.public_url}/client/{wheel.name}" if wheel else "pip install <htmlshare 客户端包>",
            public_url=s.public_url,
            open_id=user.open_id,
        )

    @app.post("/me/connect")
    def connect_ai(request: Request):
        """生成一个专用凭证，并给出一段可以直接复制给 AI 工具的话（AI 自己配置远程 MCP）。"""
        user, denied = poster(request)
        if denied:
            return denied
        token = d.new_token(user.open_id, f"AI 接入 {datetime.now():%Y-%m-%d %H:%M}")
        return web.render("secret.html", shell=shell(user, "connect"), kind="prompt", text=connect_prompt(s.public_url, token))

    @app.post("/me/tokens")
    def create_token(request: Request):
        user, denied = poster(request)
        if denied:
            return denied
        token = d.new_token(user.open_id, f"命令行 {datetime.now():%Y-%m-%d %H:%M}")
        return web.render("secret.html", shell=shell(user, "connect"), kind="token", text=token, public_url=s.public_url)

    @app.post("/me/tokens/{token_id}/revoke")
    def revoke_token(token_id: int, request: Request):
        user, denied = poster(request)
        if denied:
            return denied
        label = db.token_label(user.open_id, token_id)
        if db.revoke_token_id(user.open_id, token_id):
            service.audit(user.open_id, "token_revoke", detail=label or "", via="web")
        return done("/connect#tokens", "revoked")

    # ---------- 治理 ----------
    @app.get("/audit")
    def audit_log(request: Request, scope: str = "", before: int | None = None):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        everyone = scope == "all" and is_admin(user)
        rows = service.audit_rows(None if everyone else user.open_id, before, AUDIT_PAGE)
        return web.render(
            "audit.html",
            shell=shell(user, "audit"),
            rows=rows,
            groups=AUDIT_GROUPS,
            everyone=everyone,
            before=before,
            older=rows[-1]["id"] if len(rows) == AUDIT_PAGE else None,
            limit=AUDIT_PAGE,
        )

    @app.get("/admin/pages")
    def admin_pages(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        if denied := admin_only(user, "admin_pages"):
            return denied
        return web.render(
            "admin_pages.html",
            shell=shell(user, "admin_pages"),
            flash=flash(request),
            pages=service.console_pages(None),
            totals=db.site_totals(),
            week=db.access_summary(int(time.time()) - 7 * 86400),
            notify=s.notify,
        )

    @app.post("/admin/pages/{name}")
    def admin_page_action(name: str, request: Request, action: str = Form(...)):
        user, denied = poster(request)
        if denied:
            return denied
        if denied := admin_only(user, "admin_pages"):
            return denied
        back = ("回到全站页面", "/admin/pages")
        if action != "delete":
            return notice(user, "无法操作这个页面", "未知操作。", 400, back, "admin_pages")
        try:
            service.admin_delete(user.open_id, name)
        except HTTPException as e:
            return notice(user, "无法下线这个页面", str(e.detail), e.status_code, back, "admin_pages")
        return done("/admin/pages", "taken_down")

    @app.get("/admin/status")
    def admin_status(request: Request):
        user = d.read_session(request)
        if not user:
            return d.login_redirect(request)
        if denied := admin_only(user, "admin_status"):
            return denied
        admins = [{"key": key, "name": service.display_name(key) if key.startswith("ou_") else db.name_by_email(key)} for key in s.admins]
        return web.render(
            "admin_status.html", shell=shell(user, "admin_status"), flash=flash(request), st=service.system_status(), admins=admins
        )

    @app.post("/admin/status")
    def admin_status_action(request: Request, action: str = Form(...)):
        user, denied = poster(request)
        if denied:
            return denied
        if denied := admin_only(user, "admin_status"):
            return denied
        if action == "sync" and s.directory_sync:
            d.user_dir.sync_now()
            service.audit(user.open_id, "directory_sync", detail="手动触发", via="web")
            return done("/admin/status", "sync")
        return notice(user, "无法执行", "未知操作，或者通讯录同步已关闭。", 400, ("回到系统状态", "/admin/status"), "admin_status")
