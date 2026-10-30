"""通讯录快照：后台定时把全公司成员同步到本地，供按姓名查人。

飞书用应用身份没有"按姓名搜索用户"的接口，只能按部门逐个拉成员（每个部门一次调用），
所以放在后台做：启动时若快照过期就同步一次，之后每 interval 秒一次；
查不到人时可以 kick() 提前同步（有最小间隔，防止反复触发）。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .db import Database
from .depts import ROOT, DeptDirectory
from .feishu import DirUser, FeishuError

log = logging.getLogger("htmlshare")


class UserDirectory:
    def __init__(
        self,
        feishu,
        depts: DeptDirectory,
        db: Database,
        interval: int = 6 * 3600,
        min_kick_gap: int = 1800,
        workers: int = 4,
        retries: int = 3,
    ):
        self.feishu = feishu
        self.depts = depts
        self.db = db
        self.interval = interval
        self.min_kick_gap = min_kick_gap
        self.workers = workers
        self.retries = retries
        self._sync_lock = threading.Lock()
        self._wake = threading.Event()
        self._last_attempt = 0.0
        self._thread: threading.Thread | None = None

    # ---- 查询 ----
    def synced_at(self) -> int | None:
        return self.db.directory_synced_at()

    def ready(self) -> bool:
        return self.synced_at() is not None

    def lookup(self, name: str) -> list[DirUser]:
        return self.db.directory_lookup(name)

    def search(self, query: str, limit: int = 20) -> list[DirUser]:
        return self.db.directory_search(query, limit)

    def get(self, open_id: str) -> DirUser | None:
        return self.db.directory_get(open_id)

    def dept_paths(self, user: DirUser) -> list[str]:
        return [self.depts.path(d) for d in user.dept_ids if d != ROOT]

    def describe(self, user: DirUser) -> str:
        """张三（技术中心/运维部，zhangsan@x.com，ou_xxx），给重名提示用。"""
        bits = [*self.dept_paths(user)[:2], user.email, user.open_id]
        return f"{user.name}（{'，'.join(b for b in bits if b)}）"

    # ---- 同步 ----
    def _fetch(self, dept_id: str) -> list[DirUser]:
        for attempt in range(self.retries):
            try:
                return self.feishu.list_department_users(dept_id)
            except FeishuError:
                if attempt == self.retries - 1:
                    raise
                time.sleep(1 + attempt)
        return []

    def sync(self) -> int:
        """全量同步，返回人数。失败抛 FeishuError，且不会动旧快照（宁可旧也不要残缺）。"""
        with self._sync_lock:
            self._last_attempt = time.time()
            started = time.time()
            self.depts.refresh(force=True)
            dept_ids = [ROOT, *self.depts.ids()]
            users: dict[str, DirUser] = {}
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dir-sync") as pool:
                for batch in pool.map(self._fetch, dept_ids):
                    for u in batch:
                        old = users.get(u.open_id)
                        if old:  # 同一个人在多个部门：合并部门
                            u = DirUser(u.open_id, u.name, u.en_name, u.email, tuple(dict.fromkeys(old.dept_ids + u.dept_ids)))
                        users[u.open_id] = u
            self.db.replace_directory(list(users.values()), int(time.time()))
            log.info("通讯录同步完成：%d 人，%d 个部门，用时 %.1fs", len(users), len(dept_ids), time.time() - started)
            return len(users)

    def kick(self) -> None:
        """查不到人时调用：距上次同步尝试超过 min_kick_gap 才会提前同步（后台进行，不阻塞）。"""
        if time.time() - self._last_attempt >= self.min_kick_gap:
            self._last_attempt = time.time()
            self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="directory-sync", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            age = time.time() - (self.synced_at() or 0)
            if age >= self.interval or self._wake.is_set():
                self._wake.clear()
                try:
                    self.sync()
                except Exception:
                    log.warning("通讯录同步失败，10 分钟后重试", exc_info=True)
                    self._wake.wait(600)
                    continue
                age = 0
            self._wake.wait(max(60, self.interval - age))
