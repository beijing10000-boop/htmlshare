"""部门树缓存：按名字/路径找部门，算一个部门的全部上级（用于"含子部门"授权）。"""

from __future__ import annotations

import logging
import threading
import time

from .feishu import Dept, FeishuError

log = logging.getLogger("htmlshare")

ROOT = "0"
MAX_DEPTH = 64  # 防止脏数据里的环


class DeptDirectory:
    def __init__(self, feishu, ttl: int = 600):
        self.feishu = feishu
        self.ttl = ttl
        self._by_id: dict[str, Dept] = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def refresh(self, force: bool = False) -> None:
        """过期才拉；拉失败时有旧数据就继续用旧的，没有就抛 FeishuError。"""
        if not force and self._by_id and time.time() - self._loaded_at < self.ttl:
            return
        with self._lock:
            if not force and self._by_id and time.time() - self._loaded_at < self.ttl:
                return
            try:
                depts = self.feishu.list_departments()
            except FeishuError:
                if not self._by_id:
                    raise
                log.warning("刷新部门列表失败，继续使用 %d 秒前的缓存", int(time.time() - self._loaded_at), exc_info=True)
                self._loaded_at = time.time() - self.ttl + 60  # 一分钟后再试
                return
            self._by_id = {d.id: d for d in depts}
            self._loaded_at = time.time()
            log.info("部门列表已刷新：%d 个部门", len(self._by_id))

    def __contains__(self, dept_id: str) -> bool:
        return dept_id in self._by_id

    def ids(self) -> list[str]:
        return list(self._by_id)

    def chain(self, dept_id: str) -> list[str]:
        """部门自己 + 全部上级（不含根）。"""
        out = []
        cur = dept_id
        while cur and cur != ROOT and cur not in out and len(out) < MAX_DEPTH:
            out.append(cur)
            d = self._by_id.get(cur)
            if not d:
                break
            cur = d.parent_id
        return out

    def path(self, dept_id: str) -> str:
        names = [self._by_id[d].name for d in reversed(self.chain(dept_id)) if d in self._by_id]
        return "/".join(names) or dept_id

    def search(self, query: str, limit: int = 10) -> list[Dept]:
        """部门路径里包含关键字的（给网页输入联想用），路径短的排前面。"""
        q = query.strip().casefold()
        if not q:
            return []
        hits = [d for d in self._by_id.values() if q in self.path(d.id).casefold()]
        return sorted(hits, key=lambda d: (len(self.path(d.id)), self.path(d.id)))[:limit]

    def find(self, query: str) -> list[Dept]:
        """按 open_department_id、部门名或路径后缀（技术中心/运维部）查找。"""
        q = query.strip().strip("/")
        if q in self._by_id:
            return [self._by_id[q]]
        parts = [p.strip().casefold() for p in q.split("/") if p.strip()]
        if not parts:
            return []
        hits = []
        for d in self._by_id.values():
            if d.name.strip().casefold() != parts[-1]:
                continue
            names = [n.strip().casefold() for n in self.path(d.id).split("/")]
            if names[-len(parts) :] == parts:
                hits.append(d)
        return sorted(hits, key=lambda d: self.path(d.id))
