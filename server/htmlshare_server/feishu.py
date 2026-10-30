"""飞书开放平台：网页登录（OAuth 授权码）+ 用邮箱/手机号查 open_id。

上传和登录用同一个企业自建应用，所以两边拿到的 open_id 一致，
权限名单里直接存 open_id，访问时比对即可。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx

from .config import Settings


class FeishuError(Exception):
    pass


@dataclass
class FeishuUser:
    open_id: str
    name: str
    email: str
    tenant_key: str


@dataclass(frozen=True)
class Dept:
    id: str  # open_department_id
    name: str
    parent_id: str  # 顶级部门的 parent 是 "0"（根）


def _json(resp: httpx.Response, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise FeishuError(f"{what}失败：HTTP {resp.status_code}，返回不是 JSON") from None
    if body.get("code") != 0:
        msg = body.get("error_description") or body.get("msg") or body.get("error") or ""
        raise FeishuError(f"{what}失败：code={body.get('code')} {msg}".rstrip())
    return body


class FeishuClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=10)
        self._tenant_token = ""
        self._tenant_token_exp = 0.0
        self._lock = threading.Lock()

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {"client_id": self.s.feishu_app_id, "response_type": "code", "redirect_uri": redirect_uri, "state": state},
            quote_via=quote,
        )
        return f"{self.s.feishu_accounts_base}/open-apis/authen/v1/authorize?{query}"

    def login(self, code: str, redirect_uri: str) -> FeishuUser:
        body = _json(
            self.http.post(
                f"{self.s.feishu_open_base}/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": self.s.feishu_app_id,
                    "client_secret": self.s.feishu_app_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ),
            "换取 user_access_token ",
        )
        token = body.get("access_token") or (body.get("data") or {}).get("access_token")
        if not token:
            raise FeishuError("换取 user_access_token 失败：响应中没有 access_token")
        info = _json(
            self.http.get(
                f"{self.s.feishu_open_base}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {token}"},
            ),
            "获取用户信息",
        )["data"]
        return FeishuUser(
            open_id=info["open_id"],
            name=info.get("name") or info.get("en_name") or "",
            email=info.get("enterprise_email") or info.get("email") or "",
            tenant_key=info.get("tenant_key") or "",
        )

    def _tenant_access_token(self) -> str:
        with self._lock:
            if self._tenant_token and time.time() < self._tenant_token_exp:
                return self._tenant_token
            body = _json(
                self.http.post(
                    f"{self.s.feishu_open_base}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.s.feishu_app_id, "app_secret": self.s.feishu_app_secret},
                ),
                "获取 tenant_access_token ",
            )
            self._tenant_token = body["tenant_access_token"]
            self._tenant_token_exp = time.time() + int(body.get("expire", 7200)) - 300
            return self._tenant_token

    def resolve(self, emails: list[str], mobiles: list[str]) -> dict[str, str]:
        """邮箱/手机号 -> open_id。查不到的不出现在结果里。key 为小写邮箱或原样手机号。"""
        found: dict[str, str] = {}
        emails, mobiles = list(emails), list(mobiles)
        while emails or mobiles:
            batch_e, emails = emails[:50], emails[50:]
            batch_m, mobiles = mobiles[:50], mobiles[50:]
            body = _json(
                self.http.post(
                    f"{self.s.feishu_open_base}/open-apis/contact/v3/users/batch_get_id",
                    params={"user_id_type": "open_id"},
                    headers={"Authorization": f"Bearer {self._tenant_access_token()}"},
                    json={"emails": batch_e, "mobiles": batch_m, "include_resigned": False},
                ),
                "通过邮箱/手机号查询用户",
            )
            for item in (body.get("data") or {}).get("user_list") or []:
                if not item.get("user_id"):
                    continue
                if item.get("email"):
                    found[item["email"].lower()] = item["user_id"]
                if item.get("mobile"):
                    found[item["mobile"]] = item["user_id"]
        return found

    def _get(self, path: str, params: dict, what: str) -> dict:
        return _json(
            self.http.get(
                f"{self.s.feishu_open_base}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._tenant_access_token()}"},
            ),
            what,
        ).get("data") or {}

    def list_departments(self) -> list[Dept]:
        """通讯录权限范围内的全部部门（递归子部门）。"""
        out: list[Dept] = []
        params = {"department_id_type": "open_department_id", "fetch_child": "true", "page_size": 50}
        while True:
            data = self._get("/open-apis/contact/v3/departments/0/children", params, "获取部门列表")
            for it in data.get("items") or []:
                if (it.get("status") or {}).get("is_deleted"):
                    continue
                # 上级部门字段要单独授权；缺了就没法算"含子部门"，宁可报错也不能静默当成顶级部门
                if "parent_department_id" not in it:
                    raise FeishuError(
                        "获取部门列表失败：返回里没有 parent_department_id，请确认应用已开通 contact:department.organize:readonly"
                    )
                out.append(Dept(it["open_department_id"], it.get("name") or "", it["parent_department_id"] or "0"))
            if not data.get("has_more") or not data.get("page_token"):
                return out
            params["page_token"] = data["page_token"]

    def user_department_ids(self, open_id: str) -> list[str]:
        """用户直属的部门（不含上级部门）。"""
        data = self._get(
            f"/open-apis/contact/v3/users/{open_id}",
            {"user_id_type": "open_id", "department_id_type": "open_department_id"},
            "获取用户所属部门",
        )
        user = data.get("user") or {}
        if "department_ids" not in user:
            raise FeishuError("获取用户所属部门失败：返回里没有 department_ids，请确认应用已开通 contact:user.department:readonly")
        return list(user["department_ids"] or [])


DEV_DEPARTMENTS = [
    Dept("od-dev-tech", "技术中心", "0"),
    Dept("od-dev-ops", "运维部", "od-dev-tech"),
    Dept("od-dev-rd", "研发部", "od-dev-tech"),
    Dept("od-dev-sales", "销售部", "0"),
]
# 开发模式下按登录邮箱的前缀分部门，方便手工试：ops-xx@ → 运维部，rd-xx@ → 研发部，sales-xx@ → 销售部
DEV_EMAIL_PREFIX_DEPT = {"ops": "od-dev-ops", "rd": "od-dev-rd", "sales": "od-dev-sales"}


class FakeFeishu:
    """开发/测试用：不连飞书。登录页让你随便填一个邮箱，任何邮箱都能"查到"。

    只在 HTMLSHARE_DEV_FAKE_LOGIN=1 时启用，生产环境绝不能打开。
    """

    def __init__(
        self,
        directory: dict[str, str] | None = None,
        departments: list[Dept] | None = None,
        memberships: dict[str, list[str]] | None = None,
    ):
        # directory 给测试用：只认这些邮箱/手机号；None 表示全部能查到
        self.directory = directory
        self.departments = DEV_DEPARTMENTS if departments is None else departments
        # memberships: open_id -> 直属部门；None 表示按登录邮箱前缀分配
        self.memberships = memberships
        self._emails: dict[str, str] = {}
        self.calls: dict[str, int] = {"list_departments": 0, "user_department_ids": 0}

    @staticmethod
    def open_id_for(key: str) -> str:
        return "ou_dev_" + hashlib.sha1(key.lower().encode()).hexdigest()[:16]

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return "/auth/dev-login?" + urlencode({"state": state})

    def login(self, code: str, redirect_uri: str) -> FeishuUser:
        email, _, name = code.partition("|")
        email = email.strip().lower()
        if "@" not in email:
            raise FeishuError("开发登录：请填写邮箱")
        oid = (self.directory.get(email) if self.directory else None) or self.open_id_for(email)
        self._emails[oid] = email
        return FeishuUser(open_id=oid, name=name.strip() or email.split("@")[0], email=email, tenant_key="dev")

    def list_departments(self) -> list[Dept]:
        self.calls["list_departments"] += 1
        return list(self.departments)

    def user_department_ids(self, open_id: str) -> list[str]:
        self.calls["user_department_ids"] += 1
        if self.memberships is not None:
            return list(self.memberships.get(open_id, []))
        prefix = self._emails.get(open_id, "").split("-")[0]
        dept = DEV_EMAIL_PREFIX_DEPT.get(prefix)
        return [dept] if dept else []

    def resolve(self, emails: list[str], mobiles: list[str]) -> dict[str, str]:
        out = {}
        for key in [e.lower() for e in emails] + list(mobiles):
            if self.directory is None:
                out[key] = self.open_id_for(key)
            elif key in self.directory:
                out[key] = self.directory[key]
        return out
