"""页面相关的业务逻辑：REST 接口、远程 MCP、一次性上传、网页控制台共用。

出错一律抛 HTTPException（status + 中文 detail）；远程 MCP 那层再把它转成工具错误。
改动数据的方法都带 via（web / api / mcp），原样记进操作审计。
"""

from __future__ import annotations

import codecs
import hashlib
import html
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from . import __version__
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
USER_DEPT_TTL = 3600  # 用户所属部门缓存 1 小时（重新登录会立即刷新）
EXPIRING_DAYS = 7  # 「快到期」= 7 天内过期
TREND_DAYS = 14  # 访问趋势看最近 14 天
REASON_MAX = 200  # 访问申请的理由最多 200 字
VIA_TEXT = {"user": "直接分享给你", "all": "全员可见"}
SYSTEM = "system"  # 审计里系统自动执行的操作人

# 操作审计：动作 -> (显示名, 分组)。分组用于页面上的筛选
AUDIT_ACTIONS = {
    "publish": ("发布页面", "page"),
    "viewers": ("修改可见范围", "page"),
    "expiry": ("修改过期时间", "page"),
    "delete": ("删除页面", "page"),
    "expire": ("到期清理", "page"),
    "admin_delete": ("管理员下线", "page"),
    "request": ("申请访问", "request"),
    "request_approve": ("通过申请", "request"),
    "request_reject": ("拒绝申请", "request"),
    "token_create": ("生成凭证", "token"),
    "token_revoke": ("撤销凭证", "token"),
    "login": ("登录", "login"),
    "directory_sync": ("同步通讯录", "system"),
}
AUDIT_GROUPS = {"page": "页面", "request": "访问申请", "token": "接入凭证", "login": "登录", "system": "系统"}
VIA_LABELS = {"web": "网页", "api": "接口 / 命令行", "mcp": "AI 工具", "system": "系统"}


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


def name_from_filename(filename: str) -> str:
    """网页上传没填页面名时，用文件名里的英文数字部分；凑不出合法名字就自动生成。"""
    stem = re.sub(r"\.html?$", "", re.split(r"[\\/]", filename or "")[-1], flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:64].strip("-")
    return slug if NAME_RE.match(slug) else auto_name()


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


def acl_scope(acl: list[AclEntry]) -> str:
    """all 全员可见 / private 仅自己 / scoped 指定的人或部门。"""
    if any(e.kind == "all" for e in acl):
        return "all"
    return "scoped" if acl else "private"


def acl_diff(before: list[AclEntry], after: list[AclEntry]) -> str:
    """名单前后对比成一句话；没变化返回空串。"""
    old = {(e.kind, e.value): e.label for e in before}
    new = {(e.kind, e.value): e.label for e in after}
    added = [label for key, label in new.items() if key not in old]
    removed = [label for key, label in old.items() if key not in new]
    parts = []
    if added:
        parts.append("新增 " + "、".join(added))
    if removed:
        parts.append("移除 " + "、".join(removed))
    return "；".join(parts)


class PageService:
    def __init__(self, s: Settings, db: Database, feishu, depts: DeptDirectory, users: UserDirectory, pages_dir: Path):
        self.s = s
        self.db = db
        self.feishu = feishu
        self.depts = depts
        self.users = users
        self.pages_dir = pages_dir
        self.max_bytes = s.max_upload_mb * 1024 * 1024
        # 飞书通知放到后台线程发，别让申请 / 审批的请求等飞书；测试里换成同步执行
        self.spawn = lambda fn: threading.Thread(target=fn, name="notify", daemon=True).start()

    # ---- helpers ----
    def page_path(self, name: str) -> Path:
        return self.pages_dir / f"{name}.html"

    def display_name(self, open_id: str) -> str:
        """登录过的用户用登录时的名字；只用过凭证、没在浏览器登录过的，用通讯录里的姓名。"""
        name = self.db.user_name(open_id)
        if not name:
            u = self.users.get(open_id)
            name = u.name if u else ""
        return name

    def user_label(self, open_id: str) -> str:
        """名单里显示的写法：通讯录里有就是"张三 <zhangsan@x.com>"，否则用登录时的名字。"""
        u = self.users.get(open_id)
        if u:
            return f"{u.name} <{u.email}>" if u.email else u.name
        return self.db.user_name(open_id) or open_id

    def audit(self, actor: str, action: str, page: str = "", owner: str = "", detail: str = "", via: str = "") -> None:
        try:
            self.db.add_audit(actor, action, page, owner, detail, via)
        except sqlite3.Error:
            log.warning("写操作审计失败 %s %s %s", actor, action, page, exc_info=True)  # 审计写不进去不能让操作本身失败

    def notify(self, open_id: str, text: str) -> None:
        """飞书机器人通知（HTMLSHARE_NOTIFY=1 才发）；发不出去只记日志。"""
        if not self.s.notify:
            return

        def send() -> None:
            try:
                self.feishu.send_text(open_id, text)
            except Exception as e:  # noqa: BLE001 - 通知失败不影响业务
                log.warning("飞书通知 %s 失败: %s", open_id, e)

        self.spawn(send)

    def page_json(self, p: Page, acl: list[AclEntry] | None = None) -> dict:
        acl = self.db.get_acl(p.name) if acl is None else acl
        return {
            "name": p.name,
            "url": f"{self.s.public_url}/{p.name}.html",
            "title": p.title,
            "size": p.size,
            "owner": p.owner,
            "owner_name": self.display_name(p.owner),
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
        for name, owner in self.db.expired_pages(int(time.time())):
            self.page_path(name).unlink(missing_ok=True)
            self.db.delete_page(name)
            self.audit(SYSTEM, "expire", name, owner, "到期自动清理", "system")
            log.info("已清理过期页面 %s", name)

    def owned_page(self, name: str, owner: str) -> Page:
        page = self.db.get_page(normalize_name(name))
        if not page or page.expired():
            raise HTTPException(404, f"页面 {name} 不存在")
        if page.owner != owner:
            raise HTTPException(403, f"页面 {name} 不是你发布的")
        return page

    def can_view(self, page: Page, open_id: str) -> bool:
        """打开页面的权限判断：发布者本人、名单里的个人、全员、所在部门（含上级）。查部门失败抛 FeishuError。"""
        if page.owner == open_id or self.db.acl_allows(page.name, open_id):
            return True
        wanted = self.db.acl_dept_ids(page.name)
        return bool(wanted and wanted & self.user_dept_chain(open_id))

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

    def settle_requests(self, page: str, acl: list[AclEntry]) -> None:
        """名单变了：已经在名单里的人，待处理的申请直接算通过（按部门加进来的，等他下次打开时再结）。"""
        if any(e.kind == "all" for e in acl):
            self.db.settle_requests(page, None)
        else:
            self.db.settle_requests(page, {e.value for e in acl if e.kind == "user"})

    # ---- 发布 ----
    def publish(self, owner: str, name: str, data: bytes, viewers=None, expires_days: int | None = None, via: str = "api") -> dict:
        """viewers 为 None：新页面仅自己可见，已有页面保留原名单。"""
        name = normalize_name(name)
        self.check_content(data)
        self.purge_expired()
        existing = self.check_name(name, owner)
        if expires_days is not None:
            self.expiry_from_days(expires_days, 0)  # 先校验，别等解析完可见人才报错
        resolved = self.resolve(viewers) if viewers is not None else None
        return self._store(
            owner, name, data, existing, resolved.entries if resolved else None, resolved.unresolved if resolved else [], expires_days, via
        )

    def _store(
        self,
        owner: str,
        name: str,
        data: bytes,
        existing: Page | None,
        acl: list[AclEntry] | None,
        unresolved: list[dict],
        expires_days: int | None,
        via: str,
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
        before = self.db.get_acl(name) if existing else []
        tmp = self.page_path(name).with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, self.page_path(name))
        self.db.save_page(page, acl if acl is not None else ([] if not existing else None))
        log.info("发布 %s by %s (%d bytes)", name, owner, len(data))
        out = self.page_json(page)
        after = [AclEntry(**v) for v in out["viewers"]]
        if acl is not None:
            self.settle_requests(name, after)
        detail = f"{'更新内容' if existing else '新页面'}，{max(1, round(len(data) / 1024))} KB"
        if not existing:
            detail += "，可见：" + ("、".join(e.label for e in after) or "仅自己")
        elif diff := acl_diff(before, after):
            detail += "；可见范围：" + diff
        self.audit(owner, "publish", name, owner, detail, via)
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
        # 上传地址只有远程 MCP 的 prepare_upload 会发
        out = self._store(t["owner"], t["name"], data, existing, t["acl"], t["unresolved"], t["expires_days"], "mcp")
        out["message"] = f"已发布：{out['url']}"
        return out

    # ---- 修改 / 查询 / 删除 ----
    def update(
        self,
        owner: str,
        name: str,
        set_viewers=None,
        add_viewers=None,
        remove_viewers_=None,
        expires_days: int | None = None,
        via: str = "api",
    ) -> dict:
        page = self.owned_page(name, owner)
        unresolved: list[dict] = []
        if set_viewers is not None or add_viewers or remove_viewers_:
            before = self.db.get_acl(page.name)
            if set_viewers is not None:
                resolved = self.resolve(set_viewers)
                acl = resolved.entries
                unresolved += resolved.unresolved
            else:
                acl = list(before)
            if add_viewers:
                resolved = self.resolve(add_viewers)
                have = {(e.kind, e.value) for e in acl}
                acl += [e for e in resolved.entries if (e.kind, e.value) not in have]
                unresolved += resolved.unresolved
            if remove_viewers_:
                acl = remove_viewers(acl, split_viewers(remove_viewers_), self.users)
            self.db.replace_acl(page.name, acl)
            self.settle_requests(page.name, acl)
            if diff := acl_diff(before, acl):
                self.audit(owner, "viewers", page.name, owner, diff, via)
        if expires_days is not None:
            page.expires_at = self.expiry_from_days(expires_days, int(time.time()))
            self.db.set_expiry(page.name, page.expires_at)
            self.audit(owner, "expiry", page.name, owner, f"改为 {expires_days} 天后过期" if expires_days else "改为永不过期", via)
        out = self.page_json(page)
        out["unresolved"] = unresolved
        return out

    def list_pages(self, owner: str) -> list[dict]:
        self.purge_expired()
        mine = self.db.list_pages(owner)
        stats = self.db.view_stats([p.name for p in mine])
        acls = self.db.acl_by_page([p.name for p in mine])
        out = []
        for p in mine:
            item = self.page_json(p, acls[p.name])
            st = stats.get(p.name, {})
            item["views"] = st.get("views", 0)
            item["visitors"] = st.get("visitors", 0)
            item["last_view_at"] = iso(st.get("last_at"))
            out.append(item)
        return out

    def get_page(self, owner: str, name: str) -> dict:
        return self.page_json(self.owned_page(name, owner))

    def user_dept_chain(self, open_id: str) -> set[str]:
        """用户直属部门 + 全部上级。直属部门缓存 USER_DEPT_TTL，部门树按 DeptDirectory 的 TTL 缓存。失败抛 FeishuError。"""
        direct = self.db.cached_user_depts(open_id, USER_DEPT_TTL)
        if direct is None:
            direct = self.feishu.user_department_ids(open_id)
            self.db.save_user_depts(open_id, direct)
        self.depts.refresh()
        if any(d not in self.depts for d in direct):
            self.depts.refresh(force=True)  # 新建的部门，缓存里还没有
        out: set[str] = set()
        for d in direct:
            out.update(self.depts.chain(d))
        return out

    def shared_with(self, open_id: str) -> dict:
        """别人分享给我、我能打开的页面。判断规则和打开页面时完全一致（个人 / 全员 / 我所在部门及其上级）。"""
        chain: set[str] = set()
        dept_error = None
        if self.db.has_dept_acl():  # 没有按部门授权的页面就不用查飞书
            try:
                chain = self.user_dept_chain(open_id)
            except FeishuError as e:
                log.warning("查询 %s 所属部门失败: %s", open_id, e)
                dept_error = "查询你所在的部门时飞书接口出错，按部门授权给你的页面暂时没有列出来，稍后刷新重试"
        pages = []
        for p, via in self.db.pages_shared_with(open_id, chain, int(time.time())):
            pages.append(
                {
                    "name": p.name,
                    "url": f"{self.s.public_url}/{p.name}.html",
                    "title": p.title,
                    "owner_name": self.display_name(p.owner),
                    "via": VIA_TEXT.get(via.kind, via.label),
                    "via_kind": via.kind,
                    "updated_at": iso(p.updated_at),
                    "expires_at": iso(p.expires_at),
                    "updated_ts": p.updated_at,
                    "expires_ts": p.expires_at,
                }
            )
        return {"pages": pages, "dept_error": dept_error}

    def delete(self, owner: str, name: str, via: str = "api") -> dict:
        page = self.owned_page(name, owner)
        self.page_path(page.name).unlink(missing_ok=True)
        self.db.delete_page(page.name)
        self.audit(owner, "delete", page.name, owner, page.title, via)
        log.info("删除 %s by %s", page.name, owner)
        return {"ok": True, "name": page.name}

    # ---- 访问统计 ----
    def daily_series(self, owner: str | None = None, page: str | None = None, days: int = TREND_DAYS) -> list[dict]:
        """最近 days 天每天的打开次数和访客数（含今天，没有访问的天补 0）。"""
        start = datetime.now().date() - timedelta(days=days - 1)
        got = self.db.access_daily(int(datetime.combine(start, datetime.min.time()).timestamp()), owner=owner, page=page)
        out = []
        for i in range(days):
            day = start + timedelta(days=i)
            row = got.get(day.isoformat(), {})
            out.append({"date": day.isoformat(), "label": f"{day:%m-%d}", "views": row.get("views", 0), "visitors": row.get("visitors", 0)})
        return out

    def page_visitors(self, page: Page, limit: int = 200) -> list[dict]:
        """访客名单（不含发布者），最近来过的在前；blocked = 最近一次是被拒绝的。"""
        out = []
        for r in self.db.page_visitors(page.name, page.owner, limit):
            blocked = bool(r["last_denied"]) and (not r["last_ok"] or r["last_denied"] > r["last_ok"])
            out.append(
                {
                    "open_id": r["open_id"],
                    "name": self.display_name(r["open_id"]) or r["open_id"],
                    "views": r["views"],
                    "denied": r["denied"],
                    "last_ts": r["last_at"],
                    "blocked": blocked,
                }
            )
        return out

    def page_stats(self, owner: str, name: str) -> dict:
        """一个页面的访问情况，给 REST / MCP 用：谁看过、看了几次、谁被拒绝、谁在申请。"""
        page = self.owned_page(name, owner)
        now = int(time.time())
        total = self.db.access_summary(0, page=page.name)
        week = self.db.access_summary(now - 7 * 86400, page=page.name)
        return {
            "name": page.name,
            "url": f"{self.s.public_url}/{page.name}.html",
            "title": page.title,
            "views": total["views"],
            "visitors": total["visitors"],
            "denied": total["denied"],
            "last_view_at": iso(total["last_at"]),
            "last_7_days": {k: week[k] for k in ("views", "visitors", "denied")},
            "daily": [{k: d[k] for k in ("date", "views", "visitors")} for d in self.daily_series(page=page.name)],
            "people": [
                {"name": v["name"], "open_id": v["open_id"], "views": v["views"], "denied": v["denied"], "last_at": iso(v["last_ts"]), "blocked": v["blocked"]}
                for v in self.page_visitors(page, limit=50)
            ],
            "pending_requests": [
                {"name": r["name"], "open_id": r["open_id"], "reason": r["reason"], "requested_at": iso(r["created_ts"])}
                for r in self.owner_requests(owner, pending=True, page=page.name)
            ],
            "note": "次数和人数都不含发布者本人；blocked=true 表示这个人最近一次打开被拒绝（不在可见范围里）。",
        }

    # ---- 控制台（网页）用的数据 ----
    def _row(self, p: Page, acl: list[AclEntry], stats: dict, now: int) -> dict:
        st = stats.get(p.name, {})
        return {
            "name": p.name,
            "title": p.title,
            "url": f"{self.s.public_url}/{p.name}.html",
            "size": p.size,
            "owner": p.owner,
            "viewers": [e.as_dict() for e in acl],
            "scope": acl_scope(acl),
            "views": st.get("views", 0),
            "visitors": st.get("visitors", 0),
            "last_view_ts": st.get("last_at"),
            "created_ts": p.created_at,
            "updated_ts": p.updated_at,
            "expires_ts": p.expires_at,
            "expiring": p.expires_at is not None and p.expires_at - now <= EXPIRING_DAYS * 86400,
        }

    def console_pages(self, owner: str | None) -> list[dict]:
        """owner 发布的页面（None = 全站，管理员用），带名单和访问统计。"""
        self.purge_expired()
        pages = self.db.all_pages() if owner is None else self.db.list_pages(owner)
        names = [p.name for p in pages]
        stats, acls, now = self.db.view_stats(names), self.db.acl_by_page(names), int(time.time())
        rows = [self._row(p, acls[p.name], stats, now) for p in pages]
        if owner is None:
            cache: dict[str, str] = {}
            for row in rows:
                oid = row["owner"]
                if oid not in cache:
                    cache[oid] = self.display_name(oid) or oid
                row["owner_name"] = cache[oid]
        return rows

    def recent_access(self, owner: str, limit: int = 8) -> list[dict]:
        cache: dict[str, str] = {}
        out = []
        for r in self.db.recent_access(owner, limit):
            oid = r["open_id"]
            if oid not in cache:
                cache[oid] = self.display_name(oid) or oid
            out.append({"page": r["page"], "title": r["title"], "name": cache[oid], "at": r["at"], "allowed": bool(r["allowed"])})
        return out

    def overview(self, open_id: str) -> dict:
        pages = self.console_pages(open_id)
        shared = self.shared_with(open_id)
        now = int(time.time())
        return {
            "pages": pages,
            "all_count": sum(p["scope"] == "all" for p in pages),
            "week": self.db.access_summary(now - 7 * 86400, owner=open_id),
            "daily": self.daily_series(owner=open_id),
            "pending": self.owner_requests(open_id, pending=True),
            "expiring": sorted((p for p in pages if p["expiring"]), key=lambda p: p["expires_ts"]),
            "shared": shared["pages"],
            "dept_error": shared["dept_error"],
            "recent": self.recent_access(open_id),
        }

    def page_detail(self, owner: str, name: str) -> dict:
        """页面详情页：基本信息 + 名单 + 访问统计 + 访客 + 待处理申请。"""
        page = self.owned_page(name, owner)
        now = int(time.time())
        acl = self.db.get_acl(page.name)
        row = self._row(page, acl, self.db.view_stats([page.name]), now)
        row["total"] = self.db.access_summary(0, page=page.name)
        row["week"] = self.db.access_summary(now - 7 * 86400, page=page.name)
        row["daily"] = self.daily_series(page=page.name)
        row["people"] = self.page_visitors(page)
        row["pending"] = self.owner_requests(owner, pending=True, page=page.name)
        return row

    # ---- 访问申请 ----
    def _request_row(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "page": r["page"],
            "title": r["title"],
            "open_id": r["open_id"],
            "name": self.display_name(r["open_id"]) or r["open_id"],
            "owner_name": self.display_name(r["owner"]),
            "reason": r["reason"],
            "status": r["status"],
            "created_ts": r["created_at"],
            "decided_ts": r["decided_at"],
        }

    def owner_requests(self, owner: str, pending: bool, page: str | None = None) -> list[dict]:
        return [self._request_row(r) for r in self.db.requests_for_owner(owner, pending, page)]

    def my_requests(self, open_id: str) -> list[dict]:
        return [self._request_row(r) for r in self.db.requests_by_user(open_id)]

    def request_state(self, name: str, open_id: str) -> dict | None:
        """「没有访问权限」页面上显示：我是不是已经申请过、结果如何。"""
        r = self.db.get_request(name, open_id)
        return {"status": r["status"], "created_ts": r["created_at"], "decided_ts": r["decided_at"], "reason": r["reason"]} if r else None

    def request_access(self, open_id: str, name: str, reason: str = "") -> dict:
        page = self.db.get_page(normalize_name(name))
        if not page or page.expired():
            raise HTTPException(404, "页面不存在或已过期")
        try:
            if self.can_view(page, open_id):
                return {"status": "allowed", "created": False}  # 已经能打开了，不用申请
        except FeishuError:
            pass  # 查不了部门就按没权限处理，让他把申请提上去
        reason = " ".join(reason.split())[:REASON_MAX]
        old = self.db.get_request(page.name, open_id)
        if old and old["status"] == "pending":
            return {"status": "pending", "created": False}  # 已经在等处理，不重复打扰发布者
        self.db.save_request(page.name, open_id, reason)
        who = self.display_name(open_id) or open_id
        self.audit(open_id, "request", page.name, page.owner, reason, "web")
        title = page.title or page.name
        self.notify(
            page.owner,
            f"{who} 申请查看你发布的页面「{title}」" + (f"，理由：{reason}" if reason else "") + f"。去处理：{self.s.public_url}/requests",
        )
        return {"status": "pending", "created": True}

    def decide_request(self, owner: str, request_id: int, approve: bool, via: str = "web") -> dict:
        r = self.db.request_by_id(request_id)
        page = self.db.get_page(r["page"]) if r else None
        if not r or not page or page.expired() or page.owner != owner:
            raise HTTPException(404, "这个申请不存在（页面可能已经删除或过期）")
        if r["status"] != "pending":
            raise HTTPException(409, "这个申请已经处理过了")
        who = self.display_name(r["open_id"]) or r["open_id"]
        if approve:
            acl = self.db.get_acl(page.name)
            if not any(e.kind == "user" and e.value == r["open_id"] for e in acl):
                acl.append(AclEntry("user", r["open_id"], self.user_label(r["open_id"])))
                self.db.replace_acl(page.name, acl)
        if not self.db.decide_request(request_id, "approved" if approve else "rejected"):
            raise HTTPException(409, "这个申请已经处理过了")
        self.audit(owner, "request_approve" if approve else "request_reject", page.name, owner, who, via)
        title = page.title or page.name
        if approve:
            self.notify(r["open_id"], f"你申请查看的页面「{title}」已通过，可以打开了：{self.s.public_url}/{page.name}.html")
        else:
            contact = f"发布者 {name}" if (name := self.display_name(owner)) else "发布者"
            self.notify(r["open_id"], f"你申请查看的页面「{title}」没有通过。如有需要请直接联系{contact}。")
        return {"ok": True, "approved": approve, "name": who, "page": page.name}

    # ---- 管理员 ----
    def admin_delete(self, admin: str, name: str) -> dict:
        """管理员下线任何人的页面（记审计、通知发布者）。管理员看不了页面内容，只能下线。"""
        page = self.db.get_page(normalize_name(name))
        if not page:
            raise HTTPException(404, f"页面 {name} 不存在")
        self.page_path(page.name).unlink(missing_ok=True)
        self.db.delete_page(page.name)
        self.audit(admin, "admin_delete", page.name, page.owner, page.title, "web")
        log.info("管理员 %s 下线 %s（发布者 %s）", admin, page.name, page.owner)
        if page.owner != admin:
            by = f"管理员 {name}" if (name := self.display_name(admin)) else "管理员"
            self.notify(page.owner, f"{by}下线了你发布的页面「{page.title or page.name}」（{page.name}），链接已失效。")
        return {"ok": True, "name": page.name}

    def system_status(self) -> dict:
        totals = self.db.site_totals()
        dept_error = None
        try:
            self.depts.refresh()
        except FeishuError as e:
            dept_error = str(e)
        db_bytes = sum(f.stat().st_size for f in self.db.path.parent.glob(self.db.path.name + "*") if f.is_file())
        now = int(time.time())
        app_id = self.s.feishu_app_id
        return {
            "version": __version__,
            "public_url": self.s.public_url,
            "dev": self.s.dev_fake_login,
            "feishu_app": f"{app_id[:6]}…{app_id[-4:]}" if len(app_id) > 12 else app_id,
            "tenant_locked": bool(self.s.allowed_tenant_key),
            "notify": self.s.notify,
            "directory_sync": self.s.directory_sync,
            "directory_hours": self.s.directory_sync_hours,
            "directory_ts": self.users.synced_at(),
            "departments": len(self.depts.ids()),
            "dept_error": dept_error,
            "db_bytes": db_bytes,
            "week": self.db.access_summary(now - 7 * 86400),
            "limits": {
                "default_expire_days": self.s.default_expire_days,
                "max_expire_days": self.s.max_expire_days,
                "max_upload_mb": self.s.max_upload_mb,
                "session_days": self.s.session_days,
            },
            **totals,
        }

    def audit_rows(self, viewer: str | None, before: int | None = None, limit: int = 100) -> list[dict]:
        """操作审计，新的在前。viewer=None 看全站（管理员），否则只看和这个人有关的。"""
        cache: dict[str, str] = {SYSTEM: "系统"}

        def who(open_id: str) -> str:
            if open_id not in cache:
                cache[open_id] = self.display_name(open_id) or open_id
            return cache[open_id]

        out = []
        for r in self.db.list_audit(viewer, before, limit):
            text, group = AUDIT_ACTIONS.get(r["action"], (r["action"], "system"))
            out.append(
                {
                    "id": r["id"],
                    "at": r["at"],
                    "actor": who(r["actor"]),
                    "action": text,
                    "group": group,
                    "page": r["page"],
                    # 页面是别人的才标出发布者（自己操作自己的页面不用重复写）
                    "owner": who(r["owner"]) if r["owner"] and r["owner"] != r["actor"] else "",
                    "detail": r["detail"],
                    "via": VIA_LABELS.get(r["via"], r["via"]),
                }
            )
        return out

    # ---- 通讯录 ----
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

    def suggest(self, q: str) -> dict:
        """网页输入可见人时的联想：同事、部门、全员。value 可以原样作为可见人提交，且唯一确定。"""
        q = q.strip()
        items: list[dict] = []
        if not q:
            return {"items": items}
        if self.users.ready():
            for u in self.users.search(q, limit=8):
                paths = self.users.dept_paths(u)
                label = u.name + (f"（{paths[0]}）" if paths else "") + (f" {u.email}" if u.email else "")
                items.append({"value": u.email or u.open_id, "label": label})
        try:
            self.depts.refresh()
            for d in self.depts.search(q, limit=6):
                path = self.depts.path(d.id)
                value = f"dept:{path}" if len(self.depts.find(path)) == 1 else f"dept:{d.id}"
                items.append({"value": value, "label": f"部门：{path}（含子部门）"})
        except FeishuError:
            pass  # 部门联想失败不影响同事联想
        if "全员".startswith(q) or "所有人".startswith(q) or "all".startswith(q.lower()):
            items.append({"value": "all", "label": "全员可见"})
        return {"items": items}

    def me(self, owner: str) -> dict:
        return {"open_id": owner, "name": self.db.user_name(owner), "server": self.s.public_url}
