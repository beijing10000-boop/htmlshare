"""SQLite 存储：页面元数据、权限名单、API Token、CLI 登录请求、访问日志、访问申请、操作审计。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .feishu import DirUser

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    open_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    name       TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    charset    TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS pages_owner ON pages(owner);
-- kind: user（value=open_id）| all（value='*'）| dept（value=open_department_id，含子部门）
CREATE TABLE IF NOT EXISTS acl (
    page  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    value TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (page, kind, value)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS cli_logins (
    device_hash TEXT PRIMARY KEY,
    user_code   TEXT NOT NULL UNIQUE,
    owner       TEXT,
    token       TEXT,
    expires_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_log (
    id      INTEGER PRIMARY KEY,
    page    TEXT NOT NULL,
    open_id TEXT NOT NULL,
    at      INTEGER NOT NULL,
    allowed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS access_log_page ON access_log(page, at);
-- v2：用户直属部门缓存（只在访问按部门授权的页面时才查飞书）
CREATE TABLE IF NOT EXISTS user_depts (
    open_id    TEXT PRIMARY KEY,
    dept_ids   TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
-- v3：通讯录快照（按姓名查人用），后台定时整表替换
CREATE TABLE IF NOT EXISTS directory (
    open_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    en_name  TEXT NOT NULL DEFAULT '',
    email    TEXT NOT NULL DEFAULT '',
    dept_ids TEXT NOT NULL DEFAULT '[]',
    name_key TEXT NOT NULL,
    en_key   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS directory_name ON directory(name_key);
CREATE INDEX IF NOT EXISTS directory_en ON directory(en_key);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- v4：一次性上传地址（远程 MCP：AI 先拿地址，再用 curl 把本地文件传上来）
CREATE TABLE IF NOT EXISTS upload_tickets (
    ticket_hash  TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    name         TEXT NOT NULL,
    acl          TEXT,                       -- JSON；NULL 表示按发布规则（新页面仅自己、已有页面保留原名单）
    unresolved   TEXT NOT NULL DEFAULT '[]',
    expires_days INTEGER,
    expires_at   INTEGER NOT NULL
);
-- v5：访问申请。没权限的人在「没有访问权限」页面上申请，发布者在控制台通过或拒绝；
--     同一个人对同一个页面只有一行，被拒绝后可以再申请（回到 pending）
CREATE TABLE IF NOT EXISTS access_requests (
    id         INTEGER PRIMARY KEY,
    page       TEXT NOT NULL,
    open_id    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    UNIQUE (page, open_id)
);
-- v5：操作审计。actor 是 open_id（系统自动执行的记 'system'）；owner 是页面发布者，
--     所以别人（申请人、管理员、系统）对我的页面做了什么，我也查得到
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY,
    at     INTEGER NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    page   TEXT NOT NULL DEFAULT '',
    owner  TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    via    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_actor ON audit_log(actor, id);
CREATE INDEX IF NOT EXISTS audit_owner ON audit_log(owner, id);
"""


def name_key(name: str) -> str:
    """姓名比对用的键：去掉所有空白、忽略大小写（Zhang San = zhangsan）。"""
    return re.sub(r"\s+", "", name).casefold()


@dataclass
class Page:
    name: str
    owner: str
    title: str
    size: int
    sha256: str
    charset: str | None
    created_at: int
    updated_at: int
    expires_at: int | None

    def expired(self, now: int | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or int(time.time()))


@dataclass
class AclEntry:
    kind: str
    value: str
    label: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "label": self.label}


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            before = c.execute("PRAGMA user_version").fetchone()[0]
            c.executescript(SCHEMA)
            if 0 < before < 5:
                # v5 之前删页面不清访问记录。现在访客名单会显示给发布者，旧页面留下的记录不能算到
                # 后来同名的新页面头上：页面已经不在的、早于当前页面发布时间的，升级时清一次
                c.execute(
                    "DELETE FROM access_log WHERE NOT EXISTS "
                    "(SELECT 1 FROM pages p WHERE p.name = access_log.page AND p.created_at <= access_log.at)"
                )
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    # ---- users ----
    def upsert_user(self, open_id: str, name: str, email: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO users(open_id, name, email, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(open_id) DO UPDATE SET name=excluded.name, email=excluded.email, updated_at=excluded.updated_at",
                (open_id, name, email, int(time.time())),
            )

    def user_name(self, open_id: str) -> str:
        with self.conn() as c:
            row = c.execute("SELECT name, email FROM users WHERE open_id=?", (open_id,)).fetchone()
        if not row:
            return ""
        return row["name"] or row["email"]

    def name_by_email(self, email: str) -> str:
        """按邮箱找人名（登录过的用户，或通讯录快照里的），找不到返回空串。"""
        with self.conn() as c:
            row = c.execute("SELECT name FROM users WHERE lower(email)=? AND name<>'' LIMIT 1", (email.lower(),)).fetchone()
            row = row or c.execute("SELECT name FROM directory WHERE email=? LIMIT 1", (email.lower(),)).fetchone()
        return row["name"] if row else ""

    # ---- pages ----
    def get_page(self, name: str) -> Page | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM pages WHERE name=?", (name,)).fetchone()
        return Page(**dict(row)) if row else None

    def save_page(self, page: Page, acl: list[AclEntry] | None) -> None:
        """新建或覆盖页面；acl 为 None 时保留原权限名单。"""
        with self.conn() as c:
            c.execute(
                "INSERT INTO pages(name, owner, title, size, sha256, charset, created_at, updated_at, expires_at) "
                "VALUES(:name,:owner,:title,:size,:sha256,:charset,:created_at,:updated_at,:expires_at) "
                "ON CONFLICT(name) DO UPDATE SET title=excluded.title, size=excluded.size, sha256=excluded.sha256, "
                "charset=excluded.charset, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                page.__dict__,
            )
            if acl is not None:
                self._replace_acl(c, page.name, acl)

    def set_expiry(self, name: str, expires_at: int | None) -> None:
        with self.conn() as c:
            c.execute("UPDATE pages SET expires_at=? WHERE name=?", (expires_at, name))

    def delete_page(self, name: str) -> None:
        """连同名单、访问记录、访问申请一起删：页面名以后可能被别人用，旧记录不能算到新页面头上。"""
        with self.conn() as c:
            c.execute("DELETE FROM pages WHERE name=?", (name,))
            c.execute("DELETE FROM acl WHERE page=?", (name,))
            c.execute("DELETE FROM access_log WHERE page=?", (name,))
            c.execute("DELETE FROM access_requests WHERE page=?", (name,))

    def list_pages(self, owner: str) -> list[Page]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM pages WHERE owner=? ORDER BY updated_at DESC", (owner,)).fetchall()
        return [Page(**dict(r)) for r in rows]

    def all_pages(self, limit: int = 500) -> list[Page]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM pages ORDER BY updated_at DESC, name LIMIT ?", (limit,)).fetchall()
        return [Page(**dict(r)) for r in rows]

    def acl_by_page(self, pages: list[str]) -> dict[str, list[AclEntry]]:
        """一次取多个页面的名单（列表页用，免得每行查一次）。"""
        out: dict[str, list[AclEntry]] = {p: [] for p in pages}
        if not pages:
            return out
        marks = ",".join("?" * len(pages))
        with self.conn() as c:
            rows = c.execute(f"SELECT page, kind, value, label FROM acl WHERE page IN ({marks}) ORDER BY kind, label", pages).fetchall()
        for r in rows:
            out[r["page"]].append(AclEntry(r["kind"], r["value"], r["label"]))
        return out

    def site_totals(self) -> dict:
        """全站计数，管理员的「全站页面」「系统状态」用。"""
        with self.conn() as c:
            pages = c.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT owner) AS owners, COALESCE(SUM(size), 0) AS bytes FROM pages").fetchone()
            one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "pages": pages["n"],
                "owners": pages["owners"],
                "bytes": pages["bytes"],
                "users": one("SELECT COUNT(*) FROM users"),
                "tokens": one("SELECT COUNT(*) FROM api_tokens"),
                "directory": one("SELECT COUNT(*) FROM directory"),
                "access_rows": one("SELECT COUNT(*) FROM access_log"),
                "audit_rows": one("SELECT COUNT(*) FROM audit_log"),
                "pending_requests": one("SELECT COUNT(*) FROM access_requests WHERE status='pending'"),
            }

    def expired_pages(self, now: int) -> list[tuple[str, str]]:
        """已过期的页面 -> (页面名, 发布者)。"""
        with self.conn() as c:
            rows = c.execute("SELECT name, owner FROM pages WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)).fetchall()
        return [(r["name"], r["owner"]) for r in rows]

    # ---- acl ----
    @staticmethod
    def _replace_acl(c: sqlite3.Connection, page: str, acl: list[AclEntry]) -> None:
        c.execute("DELETE FROM acl WHERE page=?", (page,))
        c.executemany(
            "INSERT OR IGNORE INTO acl(page, kind, value, label) VALUES(?,?,?,?)",
            [(page, e.kind, e.value, e.label) for e in acl],
        )

    def replace_acl(self, page: str, acl: list[AclEntry]) -> None:
        with self.conn() as c:
            self._replace_acl(c, page, acl)

    def get_acl(self, page: str) -> list[AclEntry]:
        with self.conn() as c:
            rows = c.execute("SELECT kind, value, label FROM acl WHERE page=? ORDER BY kind, label", (page,)).fetchall()
        return [AclEntry(r["kind"], r["value"], r["label"]) for r in rows]

    def acl_allows(self, page: str, open_id: str) -> bool:
        """只看全员和个人；部门要结合用户所属部门另外判断（见 acl_dept_ids）。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM acl WHERE page=? AND ((kind='all') OR (kind='user' AND value=?)) LIMIT 1",
                (page, open_id),
            ).fetchone()
        return row is not None

    def has_dept_acl(self) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM acl WHERE kind='dept' LIMIT 1").fetchone() is not None

    def pages_shared_with(self, open_id: str, dept_ids: set[str], now: int, limit: int = 200) -> list[tuple[Page, AclEntry]]:
        """别人发布、我能打开的未过期页面 -> (页面, 命中的那条授权)。一个页面命中多条时按 个人 > 部门 > 全员 取一条。"""
        marks = ",".join("?" * len(dept_ids)) or "NULL"
        with self.conn() as c:
            rows = c.execute(
                f"SELECT p.*, a.kind AS acl_kind, a.value AS acl_value, a.label AS acl_label "
                f"FROM pages p JOIN acl a ON a.page = p.name "
                f"WHERE p.owner <> ? AND (p.expires_at IS NULL OR p.expires_at > ?) "
                f"AND ((a.kind='user' AND a.value=?) OR a.kind='all' OR (a.kind='dept' AND a.value IN ({marks}))) "
                f"ORDER BY p.updated_at DESC, p.name",
                (open_id, now, open_id, *sorted(dept_ids)),
            ).fetchall()
        rank = {"user": 0, "dept": 1, "all": 2}
        best: dict[str, tuple[Page, AclEntry]] = {}
        for r in rows:
            d = dict(r)
            entry = AclEntry(d.pop("acl_kind"), d.pop("acl_value"), d.pop("acl_label"))
            name = d["name"]
            if name not in best or rank[entry.kind] < rank[best[name][1].kind]:
                best[name] = (Page(**d), entry)
        return list(best.values())[:limit]

    def acl_dept_ids(self, page: str) -> set[str]:
        with self.conn() as c:
            rows = c.execute("SELECT value FROM acl WHERE page=? AND kind='dept'", (page,)).fetchall()
        return {r["value"] for r in rows}

    # ---- user departments cache ----
    def cached_user_depts(self, open_id: str, max_age: int) -> list[str] | None:
        with self.conn() as c:
            row = c.execute("SELECT dept_ids, fetched_at FROM user_depts WHERE open_id=?", (open_id,)).fetchone()
        if not row or time.time() - row["fetched_at"] > max_age:
            return None
        return json.loads(row["dept_ids"])

    def save_user_depts(self, open_id: str, dept_ids: list[str]) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO user_depts(open_id, dept_ids, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(open_id) DO UPDATE SET dept_ids=excluded.dept_ids, fetched_at=excluded.fetched_at",
                (open_id, json.dumps(dept_ids), int(time.time())),
            )

    def clear_user_depts(self, open_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM user_depts WHERE open_id=?", (open_id,))

    # ---- directory (通讯录快照) ----
    def replace_directory(self, users: list[DirUser], synced_at: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM directory")
            c.executemany(
                "INSERT INTO directory(open_id, name, en_name, email, dept_ids, name_key, en_key) VALUES(?,?,?,?,?,?,?)",
                [(u.open_id, u.name, u.en_name, u.email, json.dumps(list(u.dept_ids)), name_key(u.name), name_key(u.en_name)) for u in users],
            )
            c.execute(
                "INSERT INTO meta(key, value) VALUES('directory_synced_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(synced_at),),
            )

    def directory_synced_at(self) -> int | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key='directory_synced_at'").fetchone()
        return int(row["value"]) if row else None

    @staticmethod
    def _dir_user(row: sqlite3.Row) -> DirUser:
        return DirUser(row["open_id"], row["name"], row["en_name"], row["email"], tuple(json.loads(row["dept_ids"])))

    def directory_lookup(self, name: str) -> list[DirUser]:
        """姓名或英文名完全匹配（忽略空白和大小写）。"""
        key = name_key(name)
        with self.conn() as c:
            rows = c.execute("SELECT * FROM directory WHERE name_key=? OR en_key=? ORDER BY name, email", (key, key)).fetchall()
        return [self._dir_user(r) for r in rows]

    def directory_search(self, query: str, limit: int = 20) -> list[DirUser]:
        """姓名/英文名/邮箱包含关键字；完全匹配排在前面。"""
        key = name_key(query)
        like = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM directory WHERE name_key LIKE ? ESCAPE '\\' OR en_key LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\' "
                "ORDER BY (name_key=? OR en_key=?) DESC, name, email LIMIT ?",
                (like, like, like, key, key, limit),
            ).fetchall()
        return [self._dir_user(r) for r in rows]

    def directory_get(self, open_id: str) -> DirUser | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM directory WHERE open_id=?", (open_id,)).fetchone()
        return self._dir_user(row) if row else None

    # ---- api tokens ----
    def add_token(self, token_hash: str, owner: str, label: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO api_tokens(token_hash, owner, label, created_at) VALUES(?,?,?,?)",
                (token_hash, owner, label, int(time.time())),
            )

    def token_owner(self, token_hash: str) -> str | None:
        now = int(time.time())
        with self.conn() as c:
            row = c.execute("SELECT owner, last_used_at FROM api_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if not row:
                return None
            if not row["last_used_at"] or now - row["last_used_at"] > 300:
                c.execute("UPDATE api_tokens SET last_used_at=? WHERE token_hash=?", (now, token_hash))
        return row["owner"]

    def revoke_token(self, token_hash: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM api_tokens WHERE token_hash=?", (token_hash,))

    def list_tokens(self, owner: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT rowid AS id, label, created_at, last_used_at FROM api_tokens WHERE owner=? ORDER BY created_at DESC",
                (owner,),
            ).fetchall()

    def token_label(self, owner: str, token_id: int) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT label FROM api_tokens WHERE rowid=? AND owner=?", (token_id, owner)).fetchone()
        return row["label"] if row else None

    def revoke_token_id(self, owner: str, token_id: int) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM api_tokens WHERE rowid=? AND owner=?", (token_id, owner)).rowcount == 1

    # ---- upload tickets ----
    def add_upload_ticket(
        self,
        ticket_hash: str,
        owner: str,
        name: str,
        acl: list[AclEntry] | None,
        unresolved: list[dict],
        expires_days: int | None,
        expires_at: int,
    ) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM upload_tickets WHERE expires_at <= ?", (int(time.time()),))
            c.execute(
                "INSERT INTO upload_tickets(ticket_hash, owner, name, acl, unresolved, expires_days, expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    ticket_hash,
                    owner,
                    name,
                    None if acl is None else json.dumps([e.as_dict() for e in acl], ensure_ascii=False),
                    json.dumps(unresolved, ensure_ascii=False),
                    expires_days,
                    expires_at,
                ),
            )

    def get_upload_ticket(self, ticket_hash: str) -> dict | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM upload_tickets WHERE ticket_hash=? AND expires_at > ?", (ticket_hash, int(time.time()))
            ).fetchone()
        if not row:
            return None
        return {
            "owner": row["owner"],
            "name": row["name"],
            "acl": None if row["acl"] is None else [AclEntry(**e) for e in json.loads(row["acl"])],
            "unresolved": json.loads(row["unresolved"]),
            "expires_days": row["expires_days"],
        }

    def take_upload_ticket(self, ticket_hash: str) -> bool:
        """作废上传地址；只有一个请求能成功（防止同一地址被用两次）。"""
        with self.conn() as c:
            return c.execute("DELETE FROM upload_tickets WHERE ticket_hash=?", (ticket_hash,)).rowcount == 1

    # ---- cli device login ----
    def add_cli_login(self, device_hash: str, user_code: str, expires_at: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM cli_logins WHERE expires_at <= ?", (int(time.time()),))
            c.execute(
                "INSERT INTO cli_logins(device_hash, user_code, expires_at) VALUES(?,?,?)",
                (device_hash, user_code, expires_at),
            )

    def pending_cli_login(self, user_code: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM cli_logins WHERE user_code=? AND owner IS NULL AND expires_at > ?",
                (user_code, int(time.time())),
            ).fetchone()

    def approve_cli_login(self, user_code: str, owner: str, token: str) -> bool:
        with self.conn() as c:
            cur = c.execute(
                "UPDATE cli_logins SET owner=?, token=? WHERE user_code=? AND owner IS NULL AND expires_at > ?",
                (owner, token, user_code, int(time.time())),
            )
            return cur.rowcount == 1

    def take_cli_login(self, device_hash: str) -> sqlite3.Row | None:
        """返回登录请求；若已授权则同时删除（token 只能取一次）。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM cli_logins WHERE device_hash=? AND expires_at > ?",
                (device_hash, int(time.time())),
            ).fetchone()
            if row and row["owner"]:
                c.execute("DELETE FROM cli_logins WHERE device_hash=?", (device_hash,))
            return row

    # ---- access log ----
    def log_access(self, page: str, open_id: str, allowed: bool) -> None:
        now = int(time.time())
        with self.conn() as c:
            c.execute("INSERT INTO access_log(page, open_id, at, allowed) VALUES(?,?,?,?)", (page, open_id, now, int(allowed)))
            if allowed:  # 打开成功了（比如按部门加进来的），他之前提的申请就不用再等人处理
                c.execute(
                    "UPDATE access_requests SET status='approved', decided_at=? WHERE page=? AND open_id=? AND status='pending'",
                    (now, page, open_id),
                )

    # 下面的统计都不算发布者本人：他关心的是别人看了没有，自己预览不算访问
    def view_stats(self, pages: list[str]) -> dict[str, dict]:
        """每个页面被打开的次数、人数、最近一次（只算放行的）。没人看过的页面不在结果里。"""
        if not pages:
            return {}
        marks = ",".join("?" * len(pages))
        with self.conn() as c:
            rows = c.execute(
                f"SELECT a.page, COUNT(*) AS views, COUNT(DISTINCT a.open_id) AS visitors, MAX(a.at) AS last_at "
                f"FROM access_log a JOIN pages p ON p.name = a.page "
                f"WHERE a.allowed=1 AND a.open_id <> p.owner AND a.page IN ({marks}) GROUP BY a.page",
                pages,
            ).fetchall()
        return {r["page"]: {"views": r["views"], "visitors": r["visitors"], "last_at": r["last_at"]} for r in rows}

    def view_counts(self, pages: list[str]) -> dict[str, int]:
        return {name: s["views"] for name, s in self.view_stats(pages).items()}

    @staticmethod
    def _scope(owner: str | None, page: str | None) -> tuple[str, tuple]:
        """访问统计的范围：某个人发布的全部页面、某一个页面，或全站（都不给）。"""
        if page is not None:
            return "a.page = ?", (page,)
        if owner is not None:
            return "p.owner = ?", (owner,)
        return "1=1", ()

    def access_summary(self, since: int = 0, owner: str | None = None, page: str | None = None) -> dict:
        """since 之后：放行次数、访客数、被拒次数、被拒人数、最近一次放行。"""
        where, args = self._scope(owner, page)
        with self.conn() as c:
            r = c.execute(
                f"SELECT COALESCE(SUM(a.allowed), 0) AS views, "
                f"COUNT(DISTINCT CASE WHEN a.allowed=1 THEN a.open_id END) AS visitors, "
                f"COALESCE(SUM(1 - a.allowed), 0) AS denied, "
                f"COUNT(DISTINCT CASE WHEN a.allowed=0 THEN a.open_id END) AS denied_people, "
                f"MAX(CASE WHEN a.allowed=1 THEN a.at END) AS last_at "
                f"FROM access_log a JOIN pages p ON p.name = a.page "
                f"WHERE {where} AND a.open_id <> p.owner AND a.at >= ?",
                (*args, since),
            ).fetchone()
        return dict(r)

    def access_daily(self, since: int, owner: str | None = None, page: str | None = None) -> dict[str, dict]:
        """按天（服务器本地时区）的放行次数和访客数 -> {'2026-11-03': {'views': 3, 'visitors': 2}}。"""
        where, args = self._scope(owner, page)
        with self.conn() as c:
            rows = c.execute(
                f"SELECT strftime('%Y-%m-%d', a.at, 'unixepoch', 'localtime') AS day, COUNT(*) AS views, "
                f"COUNT(DISTINCT a.open_id) AS visitors "
                f"FROM access_log a JOIN pages p ON p.name = a.page "
                f"WHERE {where} AND a.allowed=1 AND a.open_id <> p.owner AND a.at >= ? GROUP BY day",
                (*args, since),
            ).fetchall()
        return {r["day"]: {"views": r["views"], "visitors": r["visitors"]} for r in rows}

    def recent_access(self, owner: str, limit: int = 10) -> list[sqlite3.Row]:
        """我发布的页面最近被谁打开过（含被拒绝的）。"""
        with self.conn() as c:
            return c.execute(
                "SELECT a.page, p.title, a.open_id, a.at, a.allowed FROM access_log a JOIN pages p ON p.name = a.page "
                "WHERE p.owner = ? AND a.open_id <> p.owner ORDER BY a.at DESC, a.id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()

    def page_visitors(self, page: str, owner: str, limit: int = 200) -> list[sqlite3.Row]:
        """一个页面的访客，每人一行：打开次数、被拒次数、最近一次及其结果。"""
        with self.conn() as c:
            return c.execute(
                "SELECT open_id, SUM(allowed) AS views, SUM(1 - allowed) AS denied, MAX(at) AS last_at, "
                "MAX(CASE WHEN allowed=1 THEN at END) AS last_ok, MAX(CASE WHEN allowed=0 THEN at END) AS last_denied "
                "FROM access_log WHERE page = ? AND open_id <> ? GROUP BY open_id ORDER BY last_at DESC LIMIT ?",
                (page, owner, limit),
            ).fetchall()

    # ---- access requests ----
    def get_request(self, page: str, open_id: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM access_requests WHERE page=? AND open_id=?", (page, open_id)).fetchone()

    def save_request(self, page: str, open_id: str, reason: str) -> None:
        """新申请，或被拒绝后再次申请（回到待处理）。"""
        with self.conn() as c:
            c.execute(
                "INSERT INTO access_requests(page, open_id, reason, status, created_at) VALUES(?,?,?,'pending',?) "
                "ON CONFLICT(page, open_id) DO UPDATE SET reason=excluded.reason, status='pending', "
                "created_at=excluded.created_at, decided_at=NULL",
                (page, open_id, reason, int(time.time())),
            )

    def request_by_id(self, request_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(
                "SELECT r.*, p.owner, p.title FROM access_requests r JOIN pages p ON p.name = r.page WHERE r.id=?",
                (request_id,),
            ).fetchone()

    def requests_for_owner(self, owner: str, pending: bool, page: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        """别人对我发布的页面提的申请：pending=True 待处理（先到的在前），False 已处理（新的在前）。"""
        cond = "r.status = 'pending'" if pending else "r.status <> 'pending'"
        order = "r.created_at, r.id" if pending else "r.decided_at DESC, r.id DESC"
        args: tuple = (owner,)
        if page is not None:
            cond += " AND r.page = ?"
            args += (page,)
        with self.conn() as c:
            return c.execute(
                f"SELECT r.*, p.owner, p.title FROM access_requests r JOIN pages p ON p.name = r.page "
                f"WHERE p.owner = ? AND {cond} ORDER BY {order} LIMIT ?",
                (*args, limit),
            ).fetchall()

    def requests_by_user(self, open_id: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT r.*, p.owner, p.title FROM access_requests r JOIN pages p ON p.name = r.page "
                "WHERE r.open_id = ? ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
                (open_id, limit),
            ).fetchall()

    def pending_request_count(self, owner: str) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM access_requests r JOIN pages p ON p.name = r.page WHERE p.owner=? AND r.status='pending'",
                (owner,),
            ).fetchone()[0]

    def decide_request(self, request_id: int, status: str) -> bool:
        """只有还在待处理的才能改；并发点两次只有一次成功。"""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE access_requests SET status=?, decided_at=? WHERE id=? AND status='pending'",
                (status, int(time.time()), request_id),
            )
            return cur.rowcount == 1

    def settle_requests(self, page: str, open_ids: set[str] | None) -> int:
        """这些人已经能看了（名单里加了他，或者他自己打开成功了），待处理的申请直接算通过。None = 所有人。"""
        if open_ids is not None and not open_ids:
            return 0
        sql = "UPDATE access_requests SET status='approved', decided_at=? WHERE page=? AND status='pending'"
        args: list = [int(time.time()), page]
        if open_ids is not None:
            sql += f" AND open_id IN ({','.join('?' * len(open_ids))})"
            args += sorted(open_ids)
        with self.conn() as c:
            return c.execute(sql, args).rowcount

    # ---- audit log ----
    def add_audit(self, actor: str, action: str, page: str = "", owner: str = "", detail: str = "", via: str = "") -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO audit_log(at, actor, action, page, owner, detail, via) VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), actor, action, page, owner, detail[:500], via),
            )

    def list_audit(self, viewer: str | None, before: int | None = None, limit: int = 100) -> list[sqlite3.Row]:
        """viewer 给了就只看和他有关的（他做的，或别人对他的页面做的）；None = 全站（管理员）。"""
        cond, args = [], []
        if viewer is not None:
            cond.append("(actor = ? OR owner = ?)")
            args += [viewer, viewer]
        if before is not None:
            cond.append("id < ?")
            args.append(before)
        where = f"WHERE {' AND '.join(cond)}" if cond else ""
        with self.conn() as c:
            return c.execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
