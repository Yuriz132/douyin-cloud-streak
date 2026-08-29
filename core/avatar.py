"""抖音好友头像本地化缓存。

抖音网页版头像 URL 带签名参数，过期后无法再访问。
同步联系人时把头像下载到本地 data/avatars/<account_id>/<hash>.<ext>，
前端通过 /avatars/... 访问本地文件，长期有效。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import requests

from .config import DEFAULT_ACCOUNT_ID, DATA_DIR

logger = logging.getLogger("douyin-cloud-streak")

_TIMEOUT = 4
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(key: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = _FILE_LOCKS[key] = threading.Lock()
        return lock


def avatar_dir(account_id: str | None = None) -> Path:
    return DATA_DIR / "avatars" / (account_id or DEFAULT_ACCOUNT_ID)


def _public_url(account_id: str, fname: str) -> str:
    return f"/avatars/{account_id}/{fname}"


def _cache_key(url: str) -> str:
    """缓存 key 只取 URL 的 path（去签名/query），保证同一头像稳定命中缓存。

    抖音头像 URL 的 query 携带 signature/expire 等时效参数，每次同步都不同；
    若直接 md5 完整 URL，缓存将永不命中，导致每次同步全部重下头像（慢且失败率高）。
    """
    path = url.split("?")[0] or url
    return hashlib.md5(path.encode("utf-8")).hexdigest()[:16]


def fetch_and_save_avatar(url: str, account_id: str | None = None) -> str | None:
    """下载头像到本地，返回可公开访问的相对路径；失败返回 None。

    URL 为空或下载失败时不抛异常，保证同步流程不受影响。
    """
    if not url or not isinstance(url, str):
        return None
    aid = account_id or DEFAULT_ACCOUNT_ID
    try:
        ext = Path(url.split("?")[0]).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        fname = f"{_cache_key(url)}{ext}"
        d = avatar_dir(aid)
        fp = d / fname
        if fp.exists() and fp.stat().st_size > 0:
            return _public_url(aid, fname)
        with _file_lock(f"{aid}:{fname}"):
            if fp.exists() and fp.stat().st_size > 0:
                return _public_url(aid, fname)
            d.mkdir(parents=True, exist_ok=True)
            content = None
            for _attempt in range(2):
                try:
                    r = requests.get(
                        url,
                        timeout=_TIMEOUT,
                        headers={
                            "Referer": "https://www.douyin.com/",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                        },
                    )
                    if r.status_code == 200 and r.content:
                        content = r.content
                        break
                except Exception:
                    pass
            if not content:
                return None
            tmp = fp.with_suffix(fp.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(fp)
        logger.info("[%s] 头像已缓存: %s", aid, fname)
        return _public_url(aid, fname)
    except Exception as e:
        logger.warning("[%s] 头像下载失败: %s", aid, str(e)[:120])
        return None
