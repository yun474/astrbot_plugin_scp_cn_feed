from __future__ import annotations

import html
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .models import SITE_BASE_URL, FeedItem


CACHE_SECONDS = 600
REQUEST_ATTEMPTS = 2
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
FEATURE_HEADINGS = {
    "精品原创SCP": "featured_scp",
    "精品原创故事": "featured_tale",
    "精品原创图书馆": "featured_library",
}
THEME_HEADING = "主题精品"


class FetchError(RuntimeError):
    pass


class HomepageFetcher:
    """抓取中文站首页，一次请求解析出全部区块。"""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache: tuple[float, dict[str, FeedItem]] | None = None

    async def fetch(self, *, fresh: bool = False) -> dict[str, FeedItem]:
        if not fresh and self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]

        report = parse_homepage(await self._get(SITE_BASE_URL + "/"))
        if not report:
            raise FetchError("首页没有解析到任何区块，页面结构可能变了")
        self._cache = (time.monotonic(), report)
        return report

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True)
        error: Exception | None = None
        for _ in range(REQUEST_ATTEMPTS):
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                error = exc
        raise FetchError(f"访问中文站失败：{error}")


def parse_homepage(page: str) -> dict[str, FeedItem]:
    soup = BeautifulSoup(page, "html.parser")
    content = soup.select_one("#page-content") or soup
    report: dict[str, FeedItem] = {}

    for panel in content.select("div.content-panel"):
        heading = _text(panel.select_one(".panel-heading"))
        key = FEATURE_HEADINGS.get(heading)
        if not key and heading.startswith(THEME_HEADING):
            key = "featured_theme"
        if key and key not in report and (item := _feature_item(panel, key, heading)):
            report[key] = item

    if item := _contest_item(content):
        report["contests"] = item
    return report


def _feature_item(panel: Tag, key: str, heading: str) -> FeedItem | None:
    link = panel.select_one(".feature-title a[href]")
    if not link:
        return None
    url = _absolute(link["href"])
    subtitle = panel.select_one(".feature-subtitle")
    authors = [_text(user) for user in subtitle.select(".printuser")] if subtitle else []
    author = "、".join(filter(None, authors)) or _text(subtitle).removeprefix("by ").strip()
    quotes = [quote for quote in panel.select(".panel-body em") if _text(quote)]
    theme = heading.partition("：")[2] if key == "featured_theme" else ""
    return FeedItem(
        section=key,
        uid=_slug(url),
        title=_text(link),
        url=url,
        author=author or None,
        summary="\n".join(_text(quote) for quote in quotes) or None,
        summary_html="<br>".join(_inline_html(quote) for quote in quotes) or None,
        tag=theme or None,
    )


def _contest_item(content: Tag) -> FeedItem | None:
    banner = content.select_one("div.summercontest")
    link = banner.select_one("a[href]") if banner else None
    if not link:
        return None
    url = _absolute(link["href"])
    slug = _slug(url)
    # 竞赛横幅前面紧挨着的公告面板里有竞赛名和小作文。
    panel = next(
        (
            node
            for node in banner.find_all_previous("div", class_="standalone")
            if any(_slug(_absolute(a["href"])) == slug for a in node.select("a[href]"))
        ),
        None,
    )
    names = [_text(a) for a in panel.select("a[href]") if _slug(_absolute(a["href"])) == slug] if panel else []
    paragraphs = [p for p in panel.find_all("p") if _text(p)] if panel else []
    image = banner.select_one("img[src]")
    return FeedItem(
        section="contests",
        uid=slug,
        title=max(names, key=len, default="") or slug.replace("-", " ").title(),
        url=url,
        summary="\n".join(_text(p) for p in paragraphs) or None,
        summary_html="<br>".join(_inline_html(p) for p in paragraphs) or None,
        image_url=_absolute(image["src"]) if image else None,
    )


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text().split()) if node else ""


def _inline_html(node: Tag) -> str:
    """保留换行与链接高亮的精简 HTML，其余标签只取文字。"""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            parts.append(html.escape(str(child)))
        elif child.name == "br":
            parts.append("<br>")
        elif child.name == "a" or "printuser" in child.get("class", []):
            if text := _text(child):
                parts.append(f'<b class="ref">{html.escape(text)}</b>')
        else:
            parts.append(_inline_html(child))
    return " ".join("".join(parts).split())


def _absolute(href: str) -> str:
    return urljoin(SITE_BASE_URL + "/", href).split("#", 1)[0]


def _slug(url: str) -> str:
    return urlparse(url).path.strip("/")
