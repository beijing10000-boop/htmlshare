"""运行配置，全部来自环境变量（见 .env.example）。"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("htmlshare")

DEFAULT_PAGE_CSP = (
    "sandbox allow-scripts allow-forms allow-modals allow-popups "
    "allow-popups-to-escape-sandbox allow-downloads; frame-ancestors 'self'"
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: Path
    secret_key: str
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_open_base: str = "https://open.feishu.cn"
    feishu_accounts_base: str = "https://accounts.feishu.cn"
    allowed_tenant_key: str = ""
    cookie_secure: bool = False
    session_days: int = 7
    default_expire_days: int = 30
    max_expire_days: int = 365
    max_upload_mb: int = 20
    page_csp: str = DEFAULT_PAGE_CSP
    client_dist: Path = Path("/app/client-dist")
    dev_fake_login: bool = False

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}/auth/callback"

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = os.environ.get("HTMLSHARE_PUBLIC_URL", "").strip().rstrip("/")
        if not public_url:
            raise RuntimeError("必须设置 HTMLSHARE_PUBLIC_URL，例如 https://share.example.com")
        dev = _bool("HTMLSHARE_DEV_FAKE_LOGIN")
        secret = os.environ.get("HTMLSHARE_SECRET_KEY", "")
        if len(secret) < 32:
            if not dev:
                raise RuntimeError("HTMLSHARE_SECRET_KEY 至少 32 个字符（可用 python -c \"import secrets;print(secrets.token_urlsafe(48))\" 生成）")
            secret = secrets.token_urlsafe(48)
            log.warning("开发模式：未设置 HTMLSHARE_SECRET_KEY，使用临时密钥，重启后登录态失效")
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not dev and not (app_id and app_secret):
            raise RuntimeError("必须设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return cls(
            public_url=public_url,
            data_dir=Path(os.environ.get("HTMLSHARE_DATA_DIR", "/data")),
            secret_key=secret,
            feishu_app_id=app_id,
            feishu_app_secret=app_secret,
            feishu_open_base=os.environ.get("FEISHU_OPEN_BASE", cls.feishu_open_base).rstrip("/"),
            feishu_accounts_base=os.environ.get("FEISHU_ACCOUNTS_BASE", cls.feishu_accounts_base).rstrip("/"),
            allowed_tenant_key=os.environ.get("FEISHU_TENANT_KEY", "").strip(),
            cookie_secure=_bool("HTMLSHARE_COOKIE_SECURE", public_url.startswith("https://")),
            session_days=_int("HTMLSHARE_SESSION_DAYS", 7),
            default_expire_days=_int("HTMLSHARE_DEFAULT_EXPIRE_DAYS", 30),
            max_expire_days=_int("HTMLSHARE_MAX_EXPIRE_DAYS", 365),
            max_upload_mb=_int("HTMLSHARE_MAX_UPLOAD_MB", 20),
            page_csp=os.environ.get("HTMLSHARE_PAGE_CSP", DEFAULT_PAGE_CSP),
            client_dist=Path(os.environ.get("HTMLSHARE_CLIENT_DIST", "/app/client-dist")),
            dev_fake_login=dev,
        )
