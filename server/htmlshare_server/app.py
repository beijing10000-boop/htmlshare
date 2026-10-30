"""HTML 分享服务。

启动：uvicorn --factory htmlshare_server.app:create_app
"""

from __future__ import annotations

import codecs
import hashlib
import html
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from . import __version__, web
from .acl import remove_viewers, resolve_viewers, split_viewers
from .config import Settings
from .db import AclEntry, Database, Page
from .feishu import FakeFeishu, FeishuClient, FeishuError

log = logging.getLogger("htmlshare")

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", re.I)
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


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_name(raw: str) -> str:
    name = raw.strip().lower()
    if name.endswith(".html"):
        name = name[:-5]
    if not NAME_RE.match(name):
        raise HTTPException(400, "页面名只能用小写字母、数字和连字符（-），1-64 个字符，不能以 - 开头或结尾")
    return name


def safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(ch) < 0x20 for ch in target):
        return "/"
    return target


def normalize_user_code(raw: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def sniff_charset(data: bytes) -> str:
    m = META_CHARSET_RE.search(data[:4096])
    if m:
        try:
            return codecs.lookup(m.group(1).decode("ascii")).name
        except LookupError:
            pass
    return "utf-8"


def extract_title(data: bytes, charset: str | None) -> str:
    m = TITLE_RE.search(data[:65536])
    if not m:
        return ""
    text = m.group(1).decode(charset or sniff_charset(data), errors="replace")
    return " ".join(html.unescape(text).split())[:200]


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
    signer = URLSafeTimedSerializer(s.secret_key, salt="htmlshare-session")
    max_bytes = s.max_upload_mb * 1024 * 1024

    app = FastAPI(title="htmlshare", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s
    app.state.db = db

    # ---------- helpers ----------
    def page_path(name: str) -> Path:
        return pages_dir / f"{name}.html"

    def iso(ts: int | None) -> str | None:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None

    def page_json(p: Page, acl: list[AclEntry] | None = None) -> dict:
        acl = db.get_acl(p.name) if acl is None else acl
        return {
            "name": p.name,
            "url": f"{s.public_url}/{p.name}.html",
            "title": p.title,
            "size": p.size,
            "owner": p.owner,
            "owner_name": db.user_name(p.owner),
            "viewers": [e.as_dict() for e in acl],
            "created_at": iso(p.created_at),
            "updated_at": iso(p.updated_at),
            "expires_at": iso(p.expires_at),
        }

    def expiry_from_days(days: int, now: int) -> int | None:
        if days < 0 or days > s.max_expire_days:
            raise HTTPException(400, f"过期天数必须在 0-{s.max_expire_days} 之间（0 表示永不过期）")
        return None if days == 0 else now + days * 86400

    def purge_expired() -> None:
        for name in db.expired_pages(int(time.time())):
            page_path(name).unlink(missing_ok=True)
            db.delete_page(name)
            log.info("已清理过期页面 %s", name)

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

    def owned_page(name: str, owner: str) -> Page:
        page = db.get_page(normalize_name(name))
        if not page or page.expired():
            raise HTTPException(404, f"页面 {name} 不存在")
        if page.owner != owner:
            raise HTTPException(403, f"页面 {name} 不是你发布的")
        return page

    def resolve_or_502(items: list[str]):
        try:
            return resolve_viewers(items, fs)
        except FeishuError as e:
            raise HTTPException(502, str(e)) from None

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
        purge_expired()
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
        wheel = client_wheel()
        install = f"pip install {s.public_url}/client/{wheel.name}" if wheel else "pip install <htmlshare 客户端包>"
        return web.home(user.name or user.email, s.public_url, rows, install)

    @app.post("/me/tokens")
    def create_web_token(request: Request):
        user = read_session(request)
        if not user:
            return login_redirect(request)
        if not origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", status=403)
        token = new_token(user.open_id, f"web {datetime.now():%Y-%m-%d}")
        return web.token_created(user.name, token, s.public_url)

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
        path = page_path(name)
        if not page or page.expired() or not path.is_file():
            return web.message("页面不存在", "页面不存在或已过期。", status=404, user_name=user.name)
        allowed = page.owner == user.open_id or db.acl_allows(name, user.open_id)
        db.log_access(name, user.open_id, allowed)
        if not allowed:
            return web.forbidden(user.name or user.email, db.user_name(page.owner), name)
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
        db.add_token(hash_secret(token), user.open_id, f"cli {datetime.now():%Y-%m-%d}")
        log.info("CLI 登录授权 %s", user.open_id)
        return web.message("授权成功", "已授权，可以回到终端继续了。这个页面可以关闭。", user_name=user.name)

    # ---------- API ----------
    @app.get("/api/me")
    def api_me(owner: str = Depends(require_token)):
        return {"open_id": owner, "name": db.user_name(owner), "server": s.public_url}

    @app.post("/api/tokens/revoke")
    def api_revoke(request: Request, owner: str = Depends(require_token)):
        db.revoke_token(hash_secret(request.headers["authorization"][7:].strip()))
        return {"ok": True}

    @app.post("/api/pages")
    def api_publish(
        owner: str = Depends(require_token),
        file: UploadFile = File(...),
        name: str = Form(...),
        viewers: str | None = Form(None),
        expires_days: int | None = Form(None),
    ):
        name = normalize_name(name)
        data = file.file.read(max_bytes + 1)
        if not data:
            raise HTTPException(400, "文件是空的")
        if len(data) > max_bytes:
            raise HTTPException(413, f"文件超过 {s.max_upload_mb} MB 上限")
        if b"\x00" in data[:4096]:
            raise HTTPException(400, "这看起来不是 HTML 文本文件")
        purge_expired()

        existing = db.get_page(name)
        if existing and existing.owner != owner:
            raise HTTPException(409, f"页面名 {name} 已被 {db.user_name(existing.owner) or '别人'} 占用，请换一个名字")

        resolved = resolve_or_502(split_viewers(viewers)) if viewers is not None else None
        now = int(time.time())
        if expires_days is not None:
            expires_at = expiry_from_days(expires_days, now)
        elif existing:
            expires_at = existing.expires_at
        else:
            expires_at = expiry_from_days(s.default_expire_days, now)

        try:
            data.decode("utf-8")
            charset = "utf-8"
        except UnicodeDecodeError:
            charset = None
        page = Page(
            name=name,
            owner=owner,
            title=extract_title(data, charset),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            charset=charset,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            expires_at=expires_at,
        )
        tmp = page_path(name).with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, page_path(name))
        db.save_page(page, resolved.entries if resolved else ([] if not existing else None))
        log.info("发布 %s by %s (%d bytes)", name, owner, len(data))
        out = page_json(page)
        out["created"] = existing is None
        out["unresolved"] = resolved.unresolved if resolved else []
        return out

    @app.get("/api/pages")
    def api_list(owner: str = Depends(require_token)):
        purge_expired()
        mine = db.list_pages(owner)
        views = db.view_counts([p.name for p in mine])
        out = []
        for p in mine:
            item = page_json(p)
            item["views"] = views.get(p.name, 0)
            out.append(item)
        return {"pages": out}

    @app.get("/api/pages/{name}")
    def api_get(name: str, owner: str = Depends(require_token)):
        return page_json(owned_page(name, owner))

    @app.patch("/api/pages/{name}")
    def api_update(name: str, body: PageUpdate, owner: str = Depends(require_token)):
        page = owned_page(name, owner)
        unresolved: list[dict] = []
        if body.set_viewers is not None or body.add_viewers or body.remove_viewers:
            if body.set_viewers is not None:
                resolved = resolve_or_502(split_viewers(body.set_viewers))
                acl = resolved.entries
                unresolved += resolved.unresolved
            else:
                acl = db.get_acl(page.name)
            if body.add_viewers:
                resolved = resolve_or_502(split_viewers(body.add_viewers))
                have = {(e.kind, e.value) for e in acl}
                acl += [e for e in resolved.entries if (e.kind, e.value) not in have]
                unresolved += resolved.unresolved
            if body.remove_viewers:
                acl = remove_viewers(acl, split_viewers(body.remove_viewers))
            db.replace_acl(page.name, acl)
        if body.expires_days is not None:
            page.expires_at = expiry_from_days(body.expires_days, int(time.time()))
            db.set_expiry(page.name, page.expires_at)
        out = page_json(page)
        out["unresolved"] = unresolved
        return out

    @app.delete("/api/pages/{name}")
    def api_delete(name: str, owner: str = Depends(require_token)):
        page = owned_page(name, owner)
        page_path(page.name).unlink(missing_ok=True)
        db.delete_page(page.name)
        log.info("删除 %s by %s", page.name, owner)
        return {"ok": True, "name": page.name}

    return app
