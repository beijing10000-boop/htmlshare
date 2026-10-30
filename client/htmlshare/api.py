"""分享服务的 HTTP 客户端，CLI 和 MCP 共用。"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Iterable

import httpx

from . import config


class HtmlShareError(Exception):
    pass


def slugify(filename: str) -> str:
    """report_Q3 final.html -> report-q3-final；纯中文文件名时生成 page-日期-随机串。"""
    stem = Path(filename).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:64].strip("-")
    return slug or f"page-{datetime.now():%Y%m%d}-{secrets.token_hex(3)}"


def join_viewers(viewers: Iterable[str] | str | None) -> str | None:
    if viewers is None:
        return None
    if isinstance(viewers, str):
        return viewers
    return ",".join(v.strip() for v in viewers if v and v.strip())


class Client:
    def __init__(self, server: str | None = None, token: str | None = None, http: httpx.Client | None = None):
        cfg = config.load()
        self.server = (server or cfg.get("server") or "").rstrip("/")
        self.token = token or cfg.get("token")
        if not self.server and http is None:
            raise HtmlShareError("还没配置服务器地址，请先运行：htmlshare login --server <分享站地址>")
        self.http = http or httpx.Client(base_url=self.server, timeout=60)

    def _request(self, method: str, path: str, *, auth: bool = True, ok_status: tuple[int, ...] = (), **kw) -> dict:
        headers = kw.pop("headers", {})
        if auth:
            if not self.token:
                raise HtmlShareError("还没登录，请先运行：htmlshare login")
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = self.http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise HtmlShareError(f"连不上分享服务 {self.server}：{e}") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 and r.status_code not in ok_status:
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, list):
                detail = "；".join(str(d.get("msg", d)) for d in detail)
            raise HtmlShareError(detail or f"分享服务返回 HTTP {r.status_code}")
        return body if isinstance(body, dict) else {}

    # ---- 登录 ----
    def start_login(self) -> dict:
        return self._request("POST", "/api/cli/start", auth=False)

    def poll_login(self, device_code: str) -> dict:
        return self._request("POST", "/api/cli/poll", auth=False, ok_status=(404,), json={"device_code": device_code})

    def revoke(self) -> None:
        self._request("POST", "/api/tokens/revoke")

    def me(self) -> dict:
        return self._request("GET", "/api/me")

    # ---- 页面 ----
    def publish(
        self,
        file_path: str | Path | None = None,
        *,
        content: str | bytes | None = None,
        name: str | None = None,
        viewers: Iterable[str] | str | None = None,
        expires_days: int | None = None,
    ) -> dict:
        if file_path:
            path = Path(file_path).expanduser()
            if not path.is_file():
                raise HtmlShareError(f"找不到文件：{path}")
            data = path.read_bytes()
            filename = path.name
        elif content:
            data = content.encode("utf-8") if isinstance(content, str) else content
            filename = f"{name or 'page'}.html"
        else:
            raise HtmlShareError("需要提供 HTML 文件路径或 HTML 内容")
        form = {"name": name or slugify(filename)}
        joined = join_viewers(viewers)
        if joined is not None:
            form["viewers"] = joined
        if expires_days is not None:
            form["expires_days"] = str(expires_days)
        return self._request("POST", "/api/pages", data=form, files={"file": (filename, data, "text/html")})

    def list_pages(self) -> list[dict]:
        return self._request("GET", "/api/pages").get("pages", [])

    def get_page(self, name: str) -> dict:
        return self._request("GET", f"/api/pages/{name}")

    def update(
        self,
        name: str,
        *,
        set_viewers: Iterable[str] | None = None,
        add_viewers: Iterable[str] | None = None,
        remove_viewers: Iterable[str] | None = None,
        expires_days: int | None = None,
    ) -> dict:
        body = {
            "set_viewers": list(set_viewers) if set_viewers is not None else None,
            "add_viewers": list(add_viewers) if add_viewers else None,
            "remove_viewers": list(remove_viewers) if remove_viewers else None,
            "expires_days": expires_days,
        }
        return self._request("PATCH", f"/api/pages/{name}", json={k: v for k, v in body.items() if v is not None})

    def delete(self, name: str) -> dict:
        return self._request("DELETE", f"/api/pages/{name}")
