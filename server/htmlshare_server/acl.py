"""把"可见人"输入（邮箱/手机号/all/open_id/部门）解析成权限名单。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .db import AclEntry

ALL_WORDS = {"all", "*", "everyone", "全员", "所有人"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MOBILE_RE = re.compile(r"^\+?\d{6,15}$")
# 部门名里可能有空格（Sales Team），所以先按标点拆，非部门项再按空白拆
SPLIT_RE = re.compile(r"[,，;；、\n]+")
DEPT_RE = re.compile(r"^(?:dept|部门)\s*[:：]\s*(.*)$", re.I | re.S)
DEPT_LABEL_PREFIX = "部门:"


def split_viewers(raw: Iterable[str] | str | None) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        for part in SPLIT_RE.split(item):
            part = part.strip()
            if not part:
                continue
            if DEPT_RE.match(part):
                out.append(part)
            else:
                out.extend(part.split())
    return out


def _mobile(item: str) -> str | None:
    m = re.sub(r"[\s-]", "", item)
    if not MOBILE_RE.match(m):
        return None
    if m.startswith("+86"):
        m = m[3:]
    return m


def _dept_query(item: str) -> str | None:
    m = DEPT_RE.match(item)
    return m.group(1).strip() if m else None


@dataclass
class Resolved:
    entries: list[AclEntry] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


def resolve_viewers(items: list[str], feishu, depts=None) -> Resolved:
    res = Resolved()
    emails: list[str] = []
    mobiles: list[str] = []
    dept_queries: list[tuple[str, str]] = []
    for item in items:
        low = item.lower()
        if low in ALL_WORDS:
            res.entries.append(AclEntry("all", "*", "全员"))
        elif (q := _dept_query(item)) is not None:
            if not q:
                res.unresolved.append({"input": item, "reason": "部门名为空，写法如 dept:运维部"})
            else:
                dept_queries.append((item, q))
        elif item.startswith("ou_"):
            res.entries.append(AclEntry("user", item, item))
        elif EMAIL_RE.match(item):
            emails.append(low)
        elif (m := _mobile(item)) is not None:
            mobiles.append(m)
        else:
            res.unresolved.append({"input": item, "reason": "无法识别，请填飞书邮箱、手机号、dept:部门名 或 all"})

    if emails or mobiles:
        found = feishu.resolve(emails, mobiles)
        for key in emails + mobiles:
            if key in found:
                res.entries.append(AclEntry("user", found[key], key))
            else:
                res.unresolved.append({"input": key, "reason": "飞书里查不到（或此人不在应用的通讯录权限范围内）"})

    if dept_queries and depts is None:
        res.unresolved += [{"input": item, "reason": "服务端没有启用部门授权"} for item, _ in dept_queries]
    elif dept_queries:
        depts.refresh()
        for item, q in dept_queries:
            hits = depts.find(q)
            if len(hits) == 1:
                res.entries.append(AclEntry("dept", hits[0].id, DEPT_LABEL_PREFIX + depts.path(hits[0].id)))
            elif not hits:
                res.unresolved.append({"input": item, "reason": "飞书里找不到这个部门（或不在应用的通讯录权限范围内）"})
            else:
                paths = "、".join(depts.path(d.id) for d in hits[:5])
                res.unresolved.append(
                    {"input": item, "reason": f"有 {len(hits)} 个同名部门：{paths}；请写完整路径，例如 dept:{depts.path(hits[0].id)}"}
                )

    seen: set[tuple[str, str]] = set()
    uniq = []
    for e in res.entries:
        if (e.kind, e.value) not in seen:
            seen.add((e.kind, e.value))
            uniq.append(e)
    res.entries = uniq
    return res


def remove_viewers(acl: list[AclEntry], items: list[str]) -> list[AclEntry]:
    """按输入原样匹配（邮箱/手机号/open_id/all/dept:路径），不需要再查飞书。"""
    drop = {i.lower() for i in items} | {m for i in items if (m := _mobile(i))}
    drop_all = bool(drop & ALL_WORDS)
    dept_drops = [q.casefold() for i in items if (q := _dept_query(i))]
    keep = []
    for e in acl:
        if e.kind == "all" and drop_all:
            continue
        if e.value.lower() in drop or e.label.lower() in drop:
            continue
        if e.kind == "dept" and dept_drops:
            path = e.label.removeprefix(DEPT_LABEL_PREFIX).casefold()
            if any(q in (e.value.casefold(), path) or path.endswith("/" + q) for q in dept_drops):
                continue
        keep.append(e)
    return keep
