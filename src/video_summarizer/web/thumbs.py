"""拉封面图：只放行几个站点的图片 CDN，按站点补 Referer（B 站、抖音的封面有防盗链）。

随拾的封面代理和存到 Obsidian 时下封面都走这里。
"""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.request import Request, urlopen

THUMB_HOSTS = ("hdslb.com", "bilivideo.com", "biliimg.com", "douyinpic.com", "douyinstatic.com",
               "byteimg.com", "bytedance.com", "ytimg.com", "googleusercontent.com")
THUMB_REFERERS = {"hdslb.com": "https://www.bilibili.com/", "bilivideo.com": "https://www.bilibili.com/",
                  "biliimg.com": "https://www.bilibili.com/", "douyinpic.com": "https://www.douyin.com/",
                  "douyinstatic.com": "https://www.douyin.com/", "byteimg.com": "https://www.douyin.com/"}
MAX_BYTES = 3 * 1024 * 1024


class ThumbNotAllowed(ValueError):
    """不在白名单里的地址。"""


def allowed_host(url: str) -> str | None:
    """命中白名单的域名后缀；不是 http(s) 或不在白名单返回 None。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return None
    return next((h for h in THUMB_HOSTS if host == h or host.endswith("." + h)), None)


def fetch(url: str, user_agent: str, timeout: float = 15) -> tuple[bytes, str]:
    """返回 (图片字节, Content-Type)。不在白名单抛 ThumbNotAllowed，网络错误和非图片照常抛。"""
    suffix = allowed_host(url)
    if suffix is None:
        raise ThumbNotAllowed(url)
    headers = {"User-Agent": user_agent}
    if suffix in THUMB_REFERERS:
        headers["Referer"] = THUMB_REFERERS[suffix]
    with urlopen(Request(url, headers=headers), timeout=timeout) as resp:  # noqa: S310 —— 域名已白名单
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        data = resp.read(MAX_BYTES)
    if not ctype.startswith("image/"):
        raise ValueError("对方返回的不是图片")
    return data, ctype
