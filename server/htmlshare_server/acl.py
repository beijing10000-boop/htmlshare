"""把"可见人"输入（邮箱/手机号/all/open_id）解析成权限名单。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .db import AclEntry

ALL_WORDS = {"all", "*", "everyone", "全员", "所有人"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MOBILE_RE = re.compile(r"^\+?\d{6,15}$")
SPLIT_RE = re.compile(r"[,，;；、\s]+")


def split_viewers(raw: Iterable[str] | str | None) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        out.extend(p for p in SPLIT_RE.split(item.strip()) if p)
    return out


def _mobile(item: str) -> str | None:
    m = re.sub(r"[\s-]", "", item)
    if not MOBILE_RE.match(m):
        return None
    if m.startswith("+86"):
        m = m[3:]
    return m


@dataclass
class Resolved:
    entries: list[AclEntry] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


def resolve_viewers(items: list[str], feishu) -> Resolved:
    res = Resolved()
    emails: list[str] = []
    mobiles: list[str] = []
    for item in items:
        low = item.lower()
        if low in ALL_WORDS:
            res.entries.append(AclEntry("all", "*", "全员"))
        elif low.startswith("dept:") or item.startswith("部门:"):
            res.unresolved.append({"input": item, "reason": "按部门授权在二期支持，目前请用 all 或逐个填邮箱"})
        elif item.startswith("ou_"):
            res.entries.append(AclEntry("user", item, item))
        elif EMAIL_RE.match(item):
            emails.append(low)
        elif (m := _mobile(item)) is not None:
            mobiles.append(m)
        else:
            res.unresolved.append({"input": item, "reason": "无法识别，请填飞书邮箱、手机号或 all"})

    if emails or mobiles:
        found = feishu.resolve(emails, mobiles)
        for key in emails + mobiles:
            if key in found:
                res.entries.append(AclEntry("user", found[key], key))
            else:
                res.unresolved.append({"input": key, "reason": "飞书里查不到（或此人不在应用的通讯录权限范围内）"})

    seen: set[tuple[str, str]] = set()
    uniq = []
    for e in res.entries:
        if (e.kind, e.value) not in seen:
            seen.add((e.kind, e.value))
            uniq.append(e)
    res.entries = uniq
    return res


def remove_viewers(acl: list[AclEntry], items: list[str]) -> list[AclEntry]:
    """按输入原样匹配（邮箱/手机号/open_id/all），不需要再查飞书。"""
    drop = {i.lower() for i in items} | {m for i in items if (m := _mobile(i))}
    drop_all = bool(drop & ALL_WORDS)
    keep = []
    for e in acl:
        if e.kind == "all" and drop_all:
            continue
        if e.value.lower() in drop or e.label.lower() in drop:
            continue
        keep.append(e)
    return keep
