"""抖音兜底：yt-dlp 打不通详情接口时，让本机浏览器替它打。

抖音的详情接口 `/aweme/v1/web/aweme/detail/` 现在要带风控参数（uifid、a_bogus、
x-secsdk-web-signature……），这些由页面里的安全 SDK 在发请求时现算，光有 cookie 不够，
yt-dlp 的 Douyin extractor 只带 cookie，所以一直报 "Fresh cookies are needed"。

这里不去逆向签名算法（改一次就废），而是用 Playwright 无头拉起本机已装的 Chrome / Edge，
打开一次 douyin.com（不登录，访客身份），在页面上下文里调 fetch——站点自己的 JS 会把签名补齐——
拿到详情 JSON 后交回 yt-dlp 的 `_parse_aweme_video_app` 解析成标准 info，下载走原来的路。
顺手把这次会话里抖音域的 cookie 合并写回 cookies_file，别的站点的 cookie 原样保留。

无头 Chrome 会被抖音的验证中间页拦住，得抹掉两个痕迹：UA 里的 "HeadlessChrome" 和
navigator.webdriver。抹掉后访客页 1~4 秒就能过。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import DownloadConfig
from .errors import DownloadError

log = logging.getLogger(__name__)

HOME_URL = "https://www.douyin.com/"
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
# 按顺序试：Playwright 的 channel 名。前两个是系统里已装的浏览器，最后是 `playwright install` 装的内核
AUTO_CHANNELS = ("chrome", "msedge", "chromium")
LAUNCH_TIMEOUT_SEC = 30
DETAIL_TIMEOUT_SEC = 25
COOKIE_DOMAIN_SUFFIX = "douyin.com"

_VIDEO_ID_RE = re.compile(r"/(?:video|note|share/video|share/note)/(\d{10,})")
_DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")


def is_douyin_url(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _DOUYIN_HOSTS)


def video_id_from_url(url: str) -> str | None:
    m = _VIDEO_ID_RE.search(urlparse(url or "").path)
    return m.group(1) if m else None


def needs_browser(exc: Exception) -> bool:
    """yt-dlp 的这两种失败才值得开浏览器：详情接口 403，或它自己判断 cookie 不新鲜。"""
    text = str(exc)
    return "Fresh cookies" in text or "HTTP Error 403" in text


def enabled(cfg: DownloadConfig) -> bool:
    return (cfg.douyin_browser or "off").strip().lower() not in {"off", "false", "no", "none", ""}


def probe_via_browser(url: str, cfg: DownloadConfig) -> dict[str, Any]:
    """浏览器拿详情 JSON，yt-dlp 解析成处理完的 info 字典（和 extract_info 的返回一致）。"""
    video_id = video_id_from_url(url) or video_id_from_url(_resolve_short_link(url, cfg))
    if not video_id:
        raise DownloadError(f"认不出抖音视频 ID: {url}")

    detail = _run_in_thread(lambda: _fetch_detail(video_id, cfg))

    from yt_dlp import YoutubeDL
    from .ytdlp_base import build_ydl_opts

    with YoutubeDL(build_ydl_opts(cfg, skip_download=True)) as ydl:
        ie = ydl.get_info_extractor("Douyin")
        info = ie._parse_aweme_video_app(detail)  # noqa: SLF001 —— 就是要复用它的字段映射
        info.update({
            "extractor": "Douyin",
            "extractor_key": "Douyin",
            "webpage_url": f"https://www.douyin.com/video/{video_id}",
        })
        return ydl.sanitize_info(ydl.process_ie_result(info, download=False))


def fetch_detail_via_browser(video_id: str, cfg: DownloadConfig) -> dict[str, Any]:
    """只要详情 JSON 本身（头像等用）。"""
    return _run_in_thread(lambda: _fetch_detail(video_id, cfg))


def _run_in_thread(fn):
    """Playwright 的同步 API 不能在跑着事件循环的线程里用；Web 服务里的调用方不一定是哪种线程，
    统一丢到一条干净线程里跑。"""
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_target, name="douyin-browser", daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _fetch_detail(video_id: str, cfg: DownloadConfig) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise DownloadError(
            "抖音详情接口被风控拦下，需要用本机浏览器兜底，但没装 playwright。"
            "跑一次 `uv sync --extra cookies`（只装 Python 包，不用下载浏览器内核）。"
        ) from exc

    started = time.monotonic()
    with sync_playwright() as p:
        browser = _launch(p, cfg)
        try:
            # 先用默认上下文探一下真实 UA，把 HeadlessChrome 换成 Chrome 再开正式的上下文
            ua = browser.new_context().new_page().evaluate("navigator.userAgent")
            context = browser.new_context(
                user_agent=ua.replace("HeadlessChrome", "Chrome"),
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=LAUNCH_TIMEOUT_SEC * 1000)

            # 页面里的安全 SDK 要跑一会儿才会给 fetch 补签名；轮询到接口通为止
            script = (
                "async (path) => { const r = await fetch(path);"
                " return [r.status, await r.text()]; }"
            )
            status, body = 0, ""
            deadline = time.monotonic() + DETAIL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                try:
                    status, body = page.evaluate(script, f"{DETAIL_PATH}?aweme_id={video_id}")
                except PlaywrightError as exc:
                    log.debug("页面 fetch 抛错，继续等：%s", exc)
                if status == 200 and body.strip():
                    break
                time.sleep(0.5)

            cookies = [c for c in context.cookies() if c.get("name") and _is_douyin_domain(c.get("domain", ""))]
        finally:
            browser.close()

    log.info("浏览器拿抖音详情 %s：HTTP %s，用时 %.1fs", video_id, status, time.monotonic() - started)
    if cfg.cookies_file and cookies:
        try:
            merge_cookies_into_file(Path(cfg.cookies_file).expanduser(), cookies)
        except OSError as exc:
            log.warning("写回 cookie 文件失败：%s", exc)

    if status != 200 or not body.strip():
        raise DownloadError(f"浏览器也没拿到抖音详情（HTTP {status}）：{body[:120]!r}")

    detail = (json.loads(body) or {}).get("aweme_detail")
    if not isinstance(detail, dict):
        raise DownloadError(f"抖音详情接口没返回 aweme_detail，视频可能已删除或需要登录: {video_id}")
    return detail


def _launch(p, cfg: DownloadConfig):
    choice = (cfg.douyin_browser or "auto").strip().lower()
    channels = AUTO_CHANNELS if choice == "auto" else (choice,)
    args = ["--disable-blink-features=AutomationControlled"]   # 去掉 navigator.webdriver
    errors: list[str] = []
    for channel in channels:
        try:
            kwargs = {} if channel == "chromium" else {"channel": channel}
            return p.chromium.launch(headless=True, args=args, timeout=LAUNCH_TIMEOUT_SEC * 1000, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 没装这个浏览器就试下一个
            errors.append(f"{channel}: {str(exc).splitlines()[0][:120]}")
    raise DownloadError(
        "找不到能用的浏览器来兜底抖音（试过 " + ", ".join(channels) + "）。"
        "装 Chrome 或 Edge，或者跑 `playwright install chromium`。\n" + "\n".join(errors)
    )


def _is_douyin_domain(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == COOKIE_DOMAIN_SUFFIX or d.endswith("." + COOKIE_DOMAIN_SUFFIX)


def merge_cookies_into_file(path: Path, cookies: list[dict[str, Any]]) -> None:
    """把浏览器给的 cookie 合并进 Netscape 文件：抖音域的整体换新，其他域（B 站等）原样保留。"""
    from yt_dlp.cookies import YoutubeDLCookieJar

    jar = YoutubeDLCookieJar(str(path))
    if path.is_file():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as exc:  # noqa: BLE001 —— 旧文件坏了就当没有
            log.warning("原 cookie 文件读不了，整个重写：%s", exc)
    for c in list(jar):
        if _is_douyin_domain(c.domain):
            jar.clear(c.domain, c.path, c.name)
    for c in cookies:
        domain = c["domain"]
        expires = c.get("expires", -1)
        jar.set_cookie(Cookie(
            version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=c.get("path") or "/", path_specified=True, secure=bool(c.get("secure")),
            expires=int(expires) if expires and expires > 0 else None,
            discard=False, comment=None, comment_url=None, rest={},
        ))
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)
    log.info("cookie 文件已更新抖音域 %d 条：%s", len(cookies), path)


def _resolve_short_link(url: str, cfg: DownloadConfig) -> str:
    """v.douyin.com/xxx 这类短链跟一次跳转拿到带视频 ID 的正式地址。"""
    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": cfg.user_agent or "Mozilla/5.0"}) as client:
            resp = client.get(url)
            return str(resp.url)
    except httpx.HTTPError as exc:
        log.debug("短链跳转失败：%s", exc)
        return url
