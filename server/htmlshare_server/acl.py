"""把"可见人"输入（邮箱/手机号/姓名/all/open_id/部门）解析成权限名单。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .db import AclEntry

ALL_WORDS = {"all", "*", "everyone", "全员", "所有人"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MOBILE_RE = re.compile(r"^\+?\d{6,15}$")
# 部门名、英文姓名里可能有空格（Sales Team / John Smith），所以先按标点拆
SPLIT_RE = re.compile(r"[,，;；、\n]+")
DEPT_RE = re.compile(r"^(?:dept|部门)\s*[:：]\s*(.*)$", re.I | re.S)
NAME_RE = re.compile(r"^(?:name|姓名)\s*[:：]\s*(.*)$", re.I | re.S)
DEPT_LABEL_PREFIX = "部门:"


def _mobile(item: str) -> str | None:
    m = re.sub(r"[\s-]", "", item)
    if not MOBILE_RE.match(m):
        return None
    if m.startswith("+86"):
        m = m[3:]
    return m


def classify(item: str) -> tuple[str, str]:
    """-> (kind, value)，kind: all | dept | name | open_id | email | mobile。认不出来的一律当姓名。"""
    if item.lower() in ALL_WORDS:
        return "all", "*"
    if m := DEPT_RE.match(item):
        return "dept", m.group(1).strip()
    if m := NAME_RE.match(item):
        return "name", m.group(1).strip()
    if item.startswith("ou_"):
        return "open_id", item
    if EMAIL_RE.match(item):
        return "email", item.lower()
    if (mobile := _mobile(item)) is not None:
        return "mobile", mobile
    return "name", item.strip()


def split_viewers(raw: Iterable[str] | str | None) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        for part in SPLIT_RE.split(item):
            tokens = part.split()
            if not tokens:
                continue
            # "a@x.com b@x.com" 拆开；"John Smith"、"dept:Sales Team" 保持整体
            if len(tokens) > 1 and all(classify(t)[0] in {"all", "open_id", "email", "mobile"} for t in tokens):
                out.extend(tokens)
            else:
                out.append(" ".join(tokens))
    return out


@dataclass
class Resolved:
    entries: list[AclEntry] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


def _user_label(open_id: str, fallback: str, directory) -> str:
    """有通讯录时显示成"张三 <zhangsan@x.com>"，方便人看。"""
    u = directory.get(open_id) if directory is not None else None
    if not u:
        return fallback
    contact = fallback if fallback != open_id else u.email
    return f"{u.name} <{contact}>" if contact else u.name


def resolve_viewers(items: list[str], feishu, depts=None, directory=None) -> Resolved:
    res = Resolved()
    emails: list[str] = []
    mobiles: list[str] = []
    dept_queries: list[tuple[str, str]] = []
    name_queries: list[tuple[str, str]] = []
    for item in items:
        kind, value = classify(item)
        if kind == "all":
            res.entries.append(AclEntry("all", "*", "全员"))
        elif kind in {"dept", "name"} and not value:
            res.unresolved.append({"input": item, "reason": "冒号后面是空的，写法如 dept:运维部、name:张三"})
        elif kind == "dept":
            dept_queries.append((item, value))
        elif kind == "name":
            name_queries.append((item, value))
        elif kind == "open_id":
            res.entries.append(AclEntry("user", value, _user_label(value, value, directory)))
        elif kind == "email":
            emails.append(value)
        else:
            mobiles.append(value)

    if emails or mobiles:
        found = feishu.resolve(emails, mobiles)
        for key in emails + mobiles:
            if key in found:
                res.entries.append(AclEntry("user", found[key], _user_label(found[key], key, directory)))
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
                    {
                        "input": item,
                        "reason": f"有 {len(hits)} 个同名部门：{paths}；请写完整路径，例如 dept:{depts.path(hits[0].id)}",
                        # value 可以原样作为可见人再提交一次
                        "candidates": [{"value": f"dept:{d.id}", "label": DEPT_LABEL_PREFIX + depts.path(d.id)} for d in hits[:10]],
                    }
                )

    if name_queries:
        if directory is None:
            res.unresolved += [{"input": item, "reason": "服务端没有启用按姓名查人，请改用邮箱"} for item, _ in name_queries]
        elif not directory.ready():
            res.unresolved += [
                {"input": item, "reason": "通讯录还在同步（服务启动后约 1 分钟），请稍后重试，或改用邮箱"} for item, _ in name_queries
            ]
        else:
            for item, q in name_queries:
                hits = directory.lookup(q)
                if len(hits) == 1:
                    res.entries.append(AclEntry("user", hits[0].open_id, _user_label(hits[0].open_id, hits[0].open_id, directory)))
                elif not hits:
                    directory.kick()
                    res.unresolved.append(
                        {"input": item, "reason": f"通讯录里没有叫“{q}”的人（新入职的同事可能还没同步到，可改用邮箱）"}
                    )
                else:
                    cands = "；".join(directory.describe(u) for u in hits[:5])
                    res.unresolved.append(
                        {
                            "input": item,
                            "reason": f"有 {len(hits)} 个人叫“{q}”：{cands}。请改用邮箱或 open_id 指定是哪一位",
                            "candidates": [{"value": u.open_id, "label": directory.describe(u, with_id=False)} for u in hits[:10]],
                        }
                    )

    seen: set[tuple[str, str]] = set()
    uniq = []
    for e in res.entries:
        if (e.kind, e.value) not in seen:
            seen.add((e.kind, e.value))
            uniq.append(e)
    res.entries = uniq
    return res


def remove_viewers(acl: list[AclEntry], items: list[str], directory=None) -> list[AclEntry]:
    """按输入匹配（邮箱/手机号/open_id/姓名/all/dept:路径），不调用飞书接口。"""
    drop = {i.lower() for i in items} | {m for i in items if (m := _mobile(i))}
    drop_all = bool(drop & ALL_WORDS)
    dept_drops = [v.casefold() for i in items if (c := classify(i))[0] == "dept" and (v := c[1])]
    names = [v for i in items if (c := classify(i))[0] == "name" and (v := c[1])]
    name_ids = {u.open_id for n in names for u in directory.lookup(n)} if directory is not None else set()
    name_keys = {re.sub(r"\s+", "", n).casefold() for n in names}
    keep = []
    for e in acl:
        if e.kind == "all" and drop_all:
            continue
        if e.value.lower() in drop or e.label.lower() in drop:
            continue
        if e.kind == "user":
            label_name, _, contact = e.label.partition(" <")
            if e.value in name_ids or re.sub(r"\s+", "", label_name).casefold() in name_keys or contact.rstrip(">").lower() in drop:
                continue
        if e.kind == "dept" and dept_drops:
            path = e.label.removeprefix(DEPT_LABEL_PREFIX).casefold()
            if any(q in (e.value.casefold(), path) or path.endswith("/" + q) for q in dept_drops):
                continue
        keep.append(e)
    return keep
