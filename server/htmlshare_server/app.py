"""HTML 分享服务。

启动：uvicorn --factory htmlshare_server.app:create_app
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.routing import Route

from . import __version__, web
from .config import Settings
from .db import Database
from .depts import DeptDirectory
from .directory import UserDirectory
from .feishu import FakeFeishu, FeishuClient, FeishuError
from .remote_mcp import build_remote_mcp, connect_prompt
from .service import NAME_RE, PageService, hash_secret

log = logging.getLogger("htmlshare")

HEAD_TAG_RE = re.compile(rb"<head(?:\s[^>]*)?>", re.I)
HTML_TAG_RE = re.compile(rb"<html(?:\s[^>]*)?>", re.I)
DOCTYPE_RE = re.compile(rb"<!doctype[^>]*>", re.I)
# sandbox 下 localStorage / sessionStorage / document.cookie 一访问就抛 SecurityError，
# AI 生成的页面常常不做 try/catch，整段脚本会挂掉。这里换成内存版，只影响本页。
STORAGE_SHIM = (
    b"<script>(function(){function m(){var d={};return{getItem:function(k){return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null},"
    b"setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},"
    b"key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}}"
    b"['localStorage','sessionStorage'].forEach(function(n){try{window[n].getItem('_')}catch(e){"
    b"try{Object.defineProperty(window,n,{value:m(),configurable:true})}catch(_){}}});"
    b"try{document.cookie}catch(e){try{Object.defineProperty(document,'cookie',{get:function(){return ''},set:function(){},configurable:true})}catch(_){}}"
    b"})();</script>"
)
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SESSION_COOKIE = "hs_session"
OAUTH_COOKIE = "hs_oauth"
CLI_LOGIN_TTL = 600
DEPT_TREE_TTL = 600  # 部门树缓存 10 分钟


@dataclass
class Viewer:
    open_id: str
    name: str
    email: str


class PageUpdate(BaseModel):
    set_viewers: list[str] | None = None
    add_viewers: list[str] | None = None
    remove_viewers: list[str] | None = None
    expires_days: int | None = None


class CliPoll(BaseModel):
    device_code: str


def safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(ch) < 0x20 for ch in target):
        return "/"
    return target


def normalize_user_code(raw: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def inject_shim(data: bytes) -> bytes:
    """把垫片插到 <head> 后面（没有就依次找 <html>、<!doctype>），保证不会让页面进入怪异模式。"""
    for rx in (HEAD_TAG_RE, HTML_TAG_RE, DOCTYPE_RE):
        m = rx.search(data, 0, 65536)
        if m:
            return data[: m.end()] + STORAGE_SHIM + data[m.end() :]
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return bom + STORAGE_SHIM + data[len(bom) :]


def create_app(settings: Settings | None = None, feishu=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = settings or Settings.from_env()
    if s.dev_fake_login:
        log.warning("!!! HTMLSHARE_DEV_FAKE_LOGIN 已开启：登录不经过飞书，任何人可冒充任何邮箱，只能用于本地开发 !!!")
    db = Database(s.data_dir / "htmlshare.db")
    pages_dir = s.data_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    fs = feishu or (FakeFeishu() if s.dev_fake_login else FeishuClient(s))
    dept_dir = DeptDirectory(fs, ttl=DEPT_TREE_TTL)
    user_dir = UserDirectory(fs, dept_dir, db, interval=s.directory_sync_hours * 3600)
    service = PageService(s, db, fs, dept_dir, user_dir, pages_dir)
    mcp_gate, mcp_server = build_remote_mcp(service, db, s)
    signer = URLSafeTimedSerializer(s.secret_key, salt="htmlshare-session")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if s.directory_sync:
            user_dir.start()  # 后台线程；快照过期（或从没同步过）时立即同步一次
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="htmlshare", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = s
    app.state.db = db
    app.state.user_dir = user_dir
    app.state.service = service

    # ---------- helpers ----------
    def read_session(request: Request) -> Viewer | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data = signer.loads(raw, max_age=s.session_days * 86400)
        except BadSignature:
            return None
        return Viewer(open_id=data["oid"], name=data.get("name", ""), email=data.get("email", ""))

    def login_redirect(request: Request) -> RedirectResponse:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/auth/login?next={quote(target, safe='')}", status_code=302)

    def origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin == s.origin

    def require_token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "缺少 API Token，请先运行 htmlshare login")
        owner = db.token_owner(hash_secret(auth[7:].strip()))
        if not owner:
            raise HTTPException(401, "API Token 无效或已撤销，请重新运行 htmlshare login")
        return owner

    def new_token(owner: str, label: str) -> str:
        token = "hs_" + secrets.token_urlsafe(32)
        db.add_token(hash_secret(token), owner, label)
        return token

    def client_wheel() -> Path | None:
        if not s.client_dist.is_dir():
            return None
        wheels = sorted(s.client_dist.glob("htmlshare-*.whl"))
        return wheels[-1] if wheels else None

    # ---------- auth ----------
    @app.get("/auth/login")
    def auth_login(next: str | None = None):
        state = secrets.token_urlsafe(16)
        resp = RedirectResponse(fs.authorize_url(s.callback_url, state), status_code=302)
        resp.set_cookie(
            OAUTH_COOKIE,
            signer.dumps({"state": state, "next": safe_next(next)}),
            max_age=600,
            httponly=True,
            secure=s.cookie_secure,
            samesite="lax",
        )
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request, state: str = "", code: str = "", error: str = "", email: str = "", name: str = ""):
        if error:
            return web.message("登录未完成", "你取消了飞书授权。<a href='/auth/login'>重新登录</a>", status=400)
        try:
            pending = signer.loads(request.cookies.get(OAUTH_COOKIE, ""), max_age=600)
        except BadSignature:
            pending = None
        if not pending or not state or not secrets.compare_digest(pending["state"], state):
            return web.message("登录已过期", "登录状态校验失败，请 <a href='/auth/login'>重新登录</a>。", status=400)
        if s.dev_fake_login and email:
            code = f"{email}|{name}"
        if not code:
            return web.message("登录失败", "飞书没有返回授权码。", status=400)
        try:
            user = fs.login(code, s.callback_url)
        except FeishuError as e:
            log.warning("飞书登录失败: %s", e)
            return web.message("登录失败", html.escape(str(e)), status=502)
        if s.allowed_tenant_key and user.tenant_key != s.allowed_tenant_key:
            return web.message("登录失败", "只允许本企业飞书账号访问。", status=403)
        db.upsert_user(user.open_id, user.name, user.email)
        db.clear_user_depts(user.open_id)  # 重新登录时顺便刷新所属部门（调岗后重登即可生效）
        log.info("登录 %s %s tenant_key=%s", user.open_id, user.name, user.tenant_key)
        resp = RedirectResponse(pending["next"], status_code=302)
        resp.delete_cookie(OAUTH_COOKIE)
        resp.set_cookie(
            SESSION_COOKIE,
            signer.dumps({"oid": user.open_id, "name": user.name, "email": user.email}),
            max_age=s.session_days * 86400,
            httponly=True,
            secure=s.cookie_secure,
            samesite="lax",
        )
        return resp

    @app.get("/auth/logout")
    def auth_logout():
        resp = web.message("已退出", "你已退出登录。<a href='/'>重新登录</a>")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    if s.dev_fake_login:

        @app.get("/auth/dev-login")
        def dev_login(state: str):
            return web.dev_login(state)

    # ---------- web ----------
    @app.get("/")
    def home(request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        service.purge_expired()
        mine = db.list_pages(user.open_id)
        views = db.view_counts([p.name for p in mine])
        rows = [
            {
                "name": p.name,
                "title": p.title,
                "viewers": [e.as_dict() for e in db.get_acl(p.name)],
                "views": views.get(p.name, 0),
                "updated_ts": p.updated_at,
                "expires_ts": p.expires_at,
            }
            for p in mine
        ]
        tokens = [dict(t) for t in db.list_tokens(user.open_id)]
        shared = service.shared_with(user.open_id)
        wheel = client_wheel()
        install = f"pip install {s.public_url}/client/{wheel.name}" if wheel else "pip install <htmlshare 客户端包>"
        return web.home(user.name or user.email, s.public_url, rows, install, tokens, shared["pages"], shared["dept_error"])

    def expiry_choices() -> list[int]:
        return [d for d in (1, 7, 30, 90, 180, 365) if d <= s.max_expire_days]

    @app.get("/me/pages/{name}")
    def page_settings_view(name: str, request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        try:
            page = service.get_page(user.open_id, name)
        except HTTPException as e:
            return web.message("无法设置这个页面", html.escape(str(e.detail)), status=e.status_code, user_name=user.name)
        return web.page_settings(user.name or user.email, page, expiry_choices())

    @app.post("/me/pages/{name}")
    def page_settings_submit(
        name: str,
        request: Request,
        action: str = Form(...),
        viewers: str = Form(""),
        value: str = Form(""),
        days: int | None = Form(None),
    ):
        """网页上改可见人 / 过期时间 / 删除；只有发布者本人能改，规则和 API、MCP 完全一样。"""
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        owner = user.open_id
        notice, unresolved, error = None, [], None
        try:
            if action == "add":
                res = service.update(owner, name, add_viewers=[viewers])
                unresolved = res["unresolved"]
                notice = "已更新可见人" if not unresolved else None
            elif action == "remove":
                service.update(owner, name, remove_viewers_=[value])
                notice = "已移除"
            elif action == "set_private":
                service.update(owner, name, set_viewers=[])
                notice = "已改为仅自己可见"
            elif action == "expiry":
                service.update(owner, name, expires_days=days if days is not None else -1)
                notice = "已修改过期时间"
            elif action == "delete":
                service.delete(owner, name)
                return RedirectResponse("/", status_code=303)
            else:
                error = "未知操作"
        except HTTPException as e:
            if e.status_code in (403, 404):
                return web.message("无法设置这个页面", html.escape(str(e.detail)), status=e.status_code, user_name=user.name)
            error = str(e.detail)
        page = service.get_page(owner, name)
        return web.page_settings(user.name or user.email, page, expiry_choices(), notice, unresolved, error)

    @app.get("/me/suggest")
    def suggest(request: Request, q: str = ""):
        if not read_session(request):
            raise HTTPException(401, "请先登录")
        return service.suggest(q)

    @app.post("/me/connect")
    def connect_ai(request: Request):
        """生成一个专用 token，并给出一段可以直接复制给 AI 工具的话（AI 自己配置远程 MCP）。"""
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        token = new_token(user.open_id, f"AI 接入 {datetime.now():%Y-%m-%d %H:%M}")
        return web.connect(user.name or user.email, connect_prompt(s.public_url, token))

    @app.post("/me/tokens")
    def create_web_token(request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        token = new_token(user.open_id, f"命令行 {datetime.now():%Y-%m-%d %H:%M}")
        return web.token_created(user.name, token, s.public_url)

    @app.post("/me/tokens/{token_id}/revoke")
    def revoke_web_token(token_id: int, request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        db.revoke_token_id(user.open_id, token_id)
        return RedirectResponse("/#tokens", status_code=303)

    @app.get("/client/{filename}")
    def client_download(filename: str):
        wheel = client_wheel()
        if not wheel or filename != wheel.name:
            raise HTTPException(404, "没有这个客户端包")
        return FileResponse(wheel, media_type="application/octet-stream", filename=wheel.name)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/{name}.html")
    def view_page(name: str, request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        name = name.lower()
        page = db.get_page(name) if NAME_RE.match(name) else None
        path = service.page_path(name)
        if not page or page.expired() or not path.is_file():
            return web.message("页面不存在", "页面不存在或已过期。", status=404, user_name=user.name)
        allowed = page.owner == user.open_id or db.acl_allows(name, user.open_id)
        if not allowed and (wanted := db.acl_dept_ids(name)):
            try:
                allowed = bool(wanted & service.user_dept_chain(user.open_id))
            except FeishuError as e:
                log.warning("查询 %s 所属部门失败: %s", user.open_id, e)
                db.log_access(name, user.open_id, False)
                return web.message("暂时无法确认权限", "这个页面按部门授权，但查询你所在的部门时飞书接口出错，请稍后刷新重试。", 503, user.name)
        db.log_access(name, user.open_id, allowed)
        if not allowed:
            return web.forbidden(user.name or user.email, service.display_name(page.owner), name)
        return Response(
            inject_shim(path.read_bytes()),
            headers={
                "Content-Type": f"text/html; charset={page.charset}" if page.charset else "text/html",
                "Content-Security-Policy": s.page_csp,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "private, no-store",
            },
        )

    # ---------- CLI 登录（类似 OAuth 设备码流程） ----------
    @app.post("/api/cli/start")
    def cli_start():
        device_code = secrets.token_urlsafe(32)
        raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        user_code = f"{raw[:4]}-{raw[4:]}"
        db.add_cli_login(hash_secret(device_code), user_code, int(time.time()) + CLI_LOGIN_TTL)
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": f"{s.public_url}/cli/verify?user_code={user_code}",
            "interval": 2,
            "expires_in": CLI_LOGIN_TTL,
        }

    @app.post("/api/cli/poll")
    def cli_poll(body: CliPoll):
        row = db.take_cli_login(hash_secret(body.device_code))
        if not row:
            return JSONResponse({"status": "expired"}, status_code=404)
        if not row["owner"]:
            return {"status": "pending"}
        return {"status": "ok", "token": row["token"], "user": db.user_name(row["owner"]), "server": s.public_url}

    @app.get("/cli/verify")
    def cli_verify_page(request: Request, user_code: str = ""):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        code = normalize_user_code(user_code)
        if not db.pending_cli_login(code):
            return web.message("授权链接无效", "这个登录请求不存在或已过期，请在终端重新运行 htmlshare login。", 404, user.name)
        return web.cli_verify(user.name or user.email, code)

    @app.post("/cli/verify")
    def cli_verify_submit(request: Request, user_code: str = Form(...)):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        code = normalize_user_code(user_code)
        token = "hs_" + secrets.token_urlsafe(32)
        if not db.approve_cli_login(code, user.open_id, token):
            return web.message("授权失败", "这个登录请求不存在或已过期，请在终端重新运行 htmlshare login。", 404, user.name)
        db.add_token(hash_secret(token), user.open_id, f"命令行 {datetime.now():%Y-%m-%d %H:%M}")
        log.info("CLI 登录授权 %s", user.open_id)
        return web.message("授权成功", "已授权，可以回到终端继续了。这个页面可以关闭。", user_name=user.name)

    # ---------- API ----------
    @app.get("/api/me")
    def api_me(owner: str = Depends(require_token)):
        return service.me(owner)

    @app.post("/api/tokens/revoke")
    def api_revoke(request: Request, owner: str = Depends(require_token)):
        db.revoke_token(hash_secret(request.headers["authorization"][7:].strip()))
        return {"ok": True}

    @app.get("/api/users/search")
    def api_user_search(q: str, owner: str = Depends(require_token)):
        """按姓名/英文名/邮箱关键字查同事，给 AI 在重名时挑人用。只返回在职员工的姓名、部门、邮箱、open_id。"""
        return service.search_users(q)

    @app.post("/api/pages")
    def api_publish(
        owner: str = Depends(require_token),
        file: UploadFile = File(...),
        name: str = Form(...),
        viewers: str | None = Form(None),
        expires_days: int | None = Form(None),
    ):
        return service.publish(owner, name, file.file.read(service.max_bytes + 1), viewers, expires_days)

    @app.api_route("/api/uploads/{ticket}", methods=["PUT", "POST"])
    async def api_upload(ticket: str, request: Request):
        """一次性上传地址（由 MCP 的 prepare_upload 生成）：请求体就是 HTML 文件本身，curl -T 即可。"""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > service.max_bytes:
            raise HTTPException(413, f"文件超过 {s.max_upload_mb} MB 上限")
        data = await request.body()
        return await run_in_threadpool(service.finish_upload, ticket, data)

    @app.get("/api/pages")
    def api_list(owner: str = Depends(require_token)):
        return {"pages": service.list_pages(owner)}

    @app.get("/api/shared-pages")
    def api_shared(owner: str = Depends(require_token)):
        """别人分享给我的页面（不放在 /api/pages/ 下，免得和名叫 shared 的页面冲突）。"""
        return service.shared_with(owner)

    @app.get("/api/pages/{name}")
    def api_get(name: str, owner: str = Depends(require_token)):
        return service.get_page(owner, name)

    @app.patch("/api/pages/{name}")
    def api_update(name: str, body: PageUpdate, owner: str = Depends(require_token)):
        return service.update(owner, name, body.set_viewers, body.add_viewers, body.remove_viewers, body.expires_days)

    @app.delete("/api/pages/{name}")
    def api_delete(name: str, owner: str = Depends(require_token)):
        return service.delete(owner, name)

    # ---------- 远程 MCP ----------
    app.router.routes.append(Route("/mcp", endpoint=mcp_gate))

    return app
