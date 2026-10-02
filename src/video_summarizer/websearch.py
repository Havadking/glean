"""联网检索：伴读提问时给模型补背景用（DESIGN.md v0.9 第 4 条）。

只在两处用：问答里模型自己决定调用（function calling）；以后的「论断核实」。
结果按（查询词, 当天日期）缓存进 SQLite，同一天同一个问题不重复花钱。
没配 key 时整个功能不出现，背景补充一律标「模型知识，未核实」。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .cache import Cache
from .config import WebSearchConfig
from .errors import VideoSummarizerError

log = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
# 每条结果给模型看的正文上限（字符）。Tavily 的 content 本身是摘要，一般几百字
_SNIPPET_CHARS = 700


class WebSearchError(VideoSummarizerError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WebSearcher:
    """一次提问用一个实例：记下这次一共检索了几次、其中几次是真花钱的。"""

    def __init__(self, cfg: WebSearchConfig, cache: Cache | None = None) -> None:
        self.cfg = cfg
        self.cache = cache
        self.calls = 0          # 模型发起的检索次数（含缓存命中）
        self.paid_calls = 0     # 真正打到检索服务的次数

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def search(self, query: str) -> list[SearchResult]:
        query = " ".join((query or "").split())[:300]
        if not query:
            return []
        self.calls += 1
        day = date.today().isoformat()
        provider = self.cfg.provider
        if self.cache is not None:
            hit = self.cache.get_web_search(provider, query, day)
            if hit is not None:
                return [SearchResult(**r) for r in hit]
        results = self._fetch(query)
        self.paid_calls += 1
        if self.cache is not None:
            self.cache.put_web_search(provider, query, day, [r.to_dict() for r in results])
        return results

    def _fetch(self, query: str) -> list[SearchResult]:
        if self.cfg.provider != "tavily":
            raise WebSearchError(f"不认识的检索服务 {self.cfg.provider!r}，目前只支持 tavily")
        key = self.cfg.api_key
        if not key:
            raise WebSearchError(f"环境变量 {self.cfg.api_key_env} 是空的，联网检索用不了")
        import httpx

        try:
            resp = httpx.post(
                _TAVILY_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"query": query, "max_results": self.cfg.max_results, "search_depth": "basic",
                      "include_answer": False},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise WebSearchError(f"联网检索请求失败：{exc}") from exc
        if resp.status_code >= 400:
            raise WebSearchError(f"联网检索返回 {resp.status_code}：{resp.text[:200]}")
        try:
            items = resp.json().get("results") or []
        except ValueError as exc:
            raise WebSearchError("联网检索返回的不是 JSON") from exc
        out = []
        for it in items:
            url = str(it.get("url") or "").strip()
            if not url:
                continue
            out.append(SearchResult(
                title=str(it.get("title") or url).strip(),
                url=url,
                content=" ".join(str(it.get("content") or "").split())[:_SNIPPET_CHARS],
            ))
        log.info("联网检索「%s」：%d 条", query, len(out))
        return out
