"""页面相关的业务逻辑：REST 接口、远程 MCP、一次性上传共用。

出错一律抛 HTTPException（status + 中文 detail）；远程 MCP 那层再把它转成工具错误。
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
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from .acl import Resolved, remove_viewers, resolve_viewers, split_viewers
from .config import Settings
from .db import AclEntry, Database, Page
from .depts import DeptDirectory
from .directory import UserDirectory
from .feishu import FeishuError

log = logging.getLogger("htmlshare")

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", re.I)
UPLOAD_TTL = 1800  # 一次性上传地址 30 分钟有效


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_name(raw: str) -> str:
    name = raw.strip().lower()
    if name.endswith(".html"):
        name = name[:-5]
    if not NAME_RE.match(name):
        raise HTTPException(400, "页面名只能用小写字母、数字和连字符（-），1-64 个字符，不能以 - 开头或结尾")
    return name


def auto_name() -> str:
    return f"page-{datetime.now():%Y%m%d}-{secrets.token_hex(3)}"


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


def iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


class PageService:
    def __init__(self, s: Settings, db: Database, feishu, depts: DeptDirectory, users: UserDirectory, pages_dir: Path):
        self.s = s
        self.db = db
        self.feishu = feishu
        self.depts = depts
        self.users = users
        self.pages_dir = pages_dir
        self.max_bytes = s.max_upload_mb * 1024 * 1024

    # ---- helpers ----
    def page_path(self, name: str) -> Path:
        return self.pages_dir / f"{name}.html"

    def page_json(self, p: Page, acl: list[AclEntry] | None = None) -> dict:
        acl = self.db.get_acl(p.name) if acl is None else acl
        return {
            "name": p.name,
            "url": f"{self.s.public_url}/{p.name}.html",
            "title": p.title,
            "size": p.size,
            "owner": p.owner,
            "owner_name": self.db.user_name(p.owner),
            "viewers": [e.as_dict() for e in acl],
            "created_at": iso(p.created_at),
            "updated_at": iso(p.updated_at),
            "expires_at": iso(p.expires_at),
        }

    def expiry_from_days(self, days: int, now: int) -> int | None:
        if days < 0 or days > self.s.max_expire_days:
            raise HTTPException(400, f"过期天数必须在 0-{self.s.max_expire_days} 之间（0 表示永不过期）")
        return None if days == 0 else now + days * 86400

    def purge_expired(self) -> None:
        for name in self.db.expired_pages(int(time.time())):
            self.page_path(name).unlink(missing_ok=True)
            self.db.delete_page(name)
            log.info("已清理过期页面 %s", name)

    def owned_page(self, name: str, owner: str) -> Page:
        page = self.db.get_page(normalize_name(name))
        if not page or page.expired():
            raise HTTPException(404, f"页面 {name} 不存在")
        if page.owner != owner:
            raise HTTPException(403, f"页面 {name} 不是你发布的")
        return page

    def resolve(self, raw) -> Resolved:
        try:
            return resolve_viewers(split_viewers(raw), self.feishu, self.depts, self.users)
        except FeishuError as e:
            raise HTTPException(502, str(e)) from None

    def check_content(self, data: bytes) -> None:
        if not data:
            raise HTTPException(400, "文件是空的")
        if len(data) > self.max_bytes:
            raise HTTPException(413, f"文件超过 {self.s.max_upload_mb} MB 上限")
        if b"\x00" in data[:4096]:
            raise HTTPException(400, "这看起来不是 HTML 文本文件")

    def check_name(self, name: str, owner: str) -> Page | None:
        """返回同名的已有页面（自己的）；被别人占用时报 409。"""
        existing = self.db.get_page(name)
        if existing and existing.owner != owner:
            raise HTTPException(409, f"页面名 {name} 已被 {self.db.user_name(existing.owner) or '别人'} 占用，请换一个名字")
        return existing

    # ---- 发布 ----
    def publish(self, owner: str, name: str, data: bytes, viewers=None, expires_days: int | None = None) -> dict:
        """viewers 为 None：新页面仅自己可见，已有页面保留原名单。"""
        name = normalize_name(name)
        self.check_content(data)
        self.purge_expired()
        existing = self.check_name(name, owner)
        if expires_days is not None:
            self.expiry_from_days(expires_days, 0)  # 先校验，别等解析完可见人才报错
        resolved = self.resolve(viewers) if viewers is not None else None
        return self._store(owner, name, data, existing, resolved.entries if resolved else None, resolved.unresolved if resolved else [], expires_days)

    def _store(
        self,
        owner: str,
        name: str,
        data: bytes,
        existing: Page | None,
        acl: list[AclEntry] | None,
        unresolved: list[dict],
        expires_days: int | None,
    ) -> dict:
        now = int(time.time())
        if expires_days is not None:
            expires_at = self.expiry_from_days(expires_days, now)
        elif existing:
            expires_at = existing.expires_at
        else:
            expires_at = self.expiry_from_days(self.s.default_expire_days, now)
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
        tmp = self.page_path(name).with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, self.page_path(name))
        self.db.save_page(page, acl if acl is not None else ([] if not existing else None))
        log.info("发布 %s by %s (%d bytes)", name, owner, len(data))
        out = self.page_json(page)
        out["created"] = existing is None
        out["unresolved"] = unresolved
        return out

    # ---- 一次性上传（远程 MCP 用） ----
    def prepare_upload(self, owner: str, name: str = "", viewers=None, expires_days: int | None = None) -> dict:
        name = normalize_name(name) if name.strip() else auto_name()
        self.purge_expired()
        self.check_name(name, owner)
        if expires_days is not None:
            self.expiry_from_days(expires_days, 0)
        resolved = self.resolve(viewers) if viewers is not None else None
        ticket = secrets.token_urlsafe(24)
        self.db.add_upload_ticket(
            hash_secret(ticket),
            owner,
            name,
            resolved.entries if resolved else None,
            resolved.unresolved if resolved else [],
            expires_days,
            int(time.time()) + UPLOAD_TTL,
        )
        url = f"{self.s.public_url}/api/uploads/{ticket}"
        return {
            "name": name,
            "page_url": f"{self.s.public_url}/{name}.html",
            "upload_url": url,
            "expires_in_seconds": UPLOAD_TTL,
            "viewers": [e.label for e in resolved.entries] if resolved else None,
            "unresolved": resolved.unresolved if resolved else [],
            "how_to_upload": {
                "bash/zsh/Git Bash": f'curl -sS -T "<HTML文件路径>" "{url}"',
                "Windows PowerShell": f'curl.exe -sS -T "<HTML文件路径>" "{url}"',
                "PowerShell（没有 curl.exe 时）": f'Invoke-RestMethod -Method Put -InFile "<HTML文件路径>" -Uri "{url}"',
            },
            "note": "上传成功后返回的 JSON 里有 url，就是访问链接。地址 30 分钟内有效、只能用一次；失败了重新调用 prepare_upload。",
        }

    def finish_upload(self, ticket: str, data: bytes) -> dict:
        h = hash_secret(ticket)
        t = self.db.get_upload_ticket(h)
        gone = "上传地址无效、已用过或已过期（30 分钟内有效、只能用一次），请重新调用 prepare_upload 获取新地址"
        if not t:
            raise HTTPException(404, gone)
        self.check_content(data)  # 内容不合格时不作废地址，修好文件可以直接重传
        if not self.db.take_upload_ticket(h):
            raise HTTPException(404, gone)
        self.purge_expired()
        existing = self.check_name(t["name"], t["owner"])
        out = self._store(t["owner"], t["name"], data, existing, t["acl"], t["unresolved"], t["expires_days"])
        out["message"] = f"已发布：{out['url']}"
        return out

    # ---- 修改 / 查询 / 删除 ----
    def update(self, owner: str, name: str, set_viewers=None, add_viewers=None, remove_viewers_=None, expires_days: int | None = None) -> dict:
        page = self.owned_page(name, owner)
        unresolved: list[dict] = []
        if set_viewers is not None or add_viewers or remove_viewers_:
            if set_viewers is not None:
                resolved = self.resolve(set_viewers)
                acl = resolved.entries
                unresolved += resolved.unresolved
            else:
                acl = self.db.get_acl(page.name)
            if add_viewers:
                resolved = self.resolve(add_viewers)
                have = {(e.kind, e.value) for e in acl}
                acl += [e for e in resolved.entries if (e.kind, e.value) not in have]
                unresolved += resolved.unresolved
            if remove_viewers_:
                acl = remove_viewers(acl, split_viewers(remove_viewers_), self.users)
            self.db.replace_acl(page.name, acl)
        if expires_days is not None:
            page.expires_at = self.expiry_from_days(expires_days, int(time.time()))
            self.db.set_expiry(page.name, page.expires_at)
        out = self.page_json(page)
        out["unresolved"] = unresolved
        return out

    def list_pages(self, owner: str) -> list[dict]:
        self.purge_expired()
        mine = self.db.list_pages(owner)
        views = self.db.view_counts([p.name for p in mine])
        out = []
        for p in mine:
            item = self.page_json(p)
            item["views"] = views.get(p.name, 0)
            out.append(item)
        return out

    def get_page(self, owner: str, name: str) -> dict:
        return self.page_json(self.owned_page(name, owner))

    def delete(self, owner: str, name: str) -> dict:
        page = self.owned_page(name, owner)
        self.page_path(page.name).unlink(missing_ok=True)
        self.db.delete_page(page.name)
        log.info("删除 %s by %s", page.name, owner)
        return {"ok": True, "name": page.name}

    def search_users(self, q: str) -> dict:
        q = q.strip()
        if not q:
            raise HTTPException(400, "请提供搜索关键字")
        synced = self.users.synced_at()
        if synced is None:
            raise HTTPException(503, "通讯录还在同步（服务启动后约 1 分钟），请稍后重试")
        users = self.users.search(q)
        if not users:
            self.users.kick()
        return {
            "synced_at": iso(synced),
            "users": [
                {"open_id": u.open_id, "name": u.name, "en_name": u.en_name, "email": u.email, "departments": self.users.dept_paths(u)}
                for u in users
            ],
        }

    def me(self, owner: str) -> dict:
        return {"open_id": owner, "name": self.db.user_name(owner), "server": self.s.public_url}
