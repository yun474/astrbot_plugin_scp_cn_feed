from __future__ import annotations

from collections import OrderedDict
import email.utils
import time
from datetime import timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

from .models import FeedItem, FeedSource, SITE_BASE_URL, SITE_NAME, tag_feed_url, tag_page_url


REQUEST_TIMEOUT_SECONDS = 25
CACHE_TTL_SECONDS = 3600
MAX_CACHE_ENTRIES = 64
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    "Cache-Control": "no-cache",
}


class WikidotApiError(RuntimeError):
    pass


EXPECTED_FETCH_ERRORS = (
    WikidotApiError,
    ValueError,
    UnicodeError,
    DefusedXmlException,
    ElementTree.ParseError,
)


class WikidotApiClient:
    """Read public Wikidot RSS feeds with a lightweight tag-page fallback.

    RSS is preferred because it is a subscription format. Wikidot sometimes
    closes RSS connections for non-browser clients, so a tag page fallback is
    used before giving up. The fallback only reads list links from tag pages.
    """

    def __init__(self, site_name: str = SITE_NAME):
        self.site_name = site_name
        self._response_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch_source(
        self,
        source: FeedSource,
        limit: int = 10,
        use_cache: bool = True,
    ) -> list[FeedItem]:
        return await self._fetch_source(source, limit, self._get_client(), use_cache)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        return self._client

    async def _fetch_source(
        self,
        source: FeedSource,
        limit: int,
        client: Any,
        use_cache: bool,
    ) -> list[FeedItem]:
        items: dict[str, FeedItem] = {}
        errors: list[str] = []

        try:
            for item in await self._fetch_homepage_source(source, client, use_cache):
                items[item.item_id] = item
        except EXPECTED_FETCH_ERRORS as exc:
            errors.append(str(exc))

        if items:
            sorted_items = sorted(items.values(), key=lambda item: item.sort_time, reverse=True)
            return sorted_items[:limit]

        for tag in source.feed_tags:
            try:
                for item in await self._fetch_feed(
                    tag_feed_url(tag, f"{source.key}_{tag}"),
                    source,
                    tag,
                    client,
                    use_cache,
                ):
                    items[item.item_id] = item
                continue
            except EXPECTED_FETCH_ERRORS as exc:
                errors.append(str(exc))

            try:
                for item in await self._fetch_tag_page(
                    tag_page_url(tag),
                    source,
                    tag,
                    client,
                    use_cache,
                ):
                    items[item.item_id] = item
            except EXPECTED_FETCH_ERRORS as exc:
                errors.append(str(exc))

        if not items and errors:
            raise WikidotApiError("; ".join(errors[-3:]))

        sorted_items = sorted(items.values(), key=lambda item: item.sort_time, reverse=True)
        return sorted_items[:limit]

    async def _fetch_homepage_source(self, source: FeedSource, client: Any, use_cache: bool) -> list[FeedItem]:
        body = await self._get_bytes(SITE_BASE_URL + "/", f"homepage failed for {source.key}", client, use_cache)
        return self._parse_homepage_source(body, source)

    def _parse_homepage_source(self, body: bytes, source: FeedSource) -> list[FeedItem]:
        root = _parse_html_tree(body)
        if source.key == "contests":
            item = _extract_homepage_contest(root, source)
            return [item] if item else []
        if source.homepage_heading:
            item = _extract_feature_panel(root, source, source.homepage_heading)
            return [item] if item else []
        return []

    async def _fetch_feed(
        self,
        url: str,
        source: FeedSource,
        tag: str,
        client: Any,
        use_cache: bool,
    ) -> list[FeedItem]:
        body = await self._get_bytes(url, f"RSS feed failed for {source.key}/{tag}", client, use_cache)
        return self._parse_feed_bytes(body, source, tag)

    async def _fetch_tag_page(
        self,
        url: str,
        source: FeedSource,
        tag: str,
        client: Any,
        use_cache: bool,
    ) -> list[FeedItem]:
        body = await self._get_bytes(url, f"tag page failed for {source.key}/{tag}", client, use_cache)
        return self._parse_tag_page(body, source, tag)

    async def _get_bytes(self, url: str, error_prefix: str, client: Any, use_cache: bool) -> bytes:
        if use_cache and (cached := self._cached_bytes(url)) is not None:
            return cached

        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WikidotApiError(f"{error_prefix}: {exc}") from exc
        if use_cache:
            self._cache_bytes(url, response.content)
        return response.content

    def _cached_bytes(self, url: str) -> bytes | None:
        cached = self._response_cache.get(url)
        if not cached:
            return None

        expires_at, body = cached
        if expires_at > time.monotonic():
            self._response_cache.move_to_end(url)
            return body

        self._response_cache.pop(url, None)
        return None

    def _cache_bytes(self, url: str, body: bytes) -> None:
        now = time.monotonic()
        expires_at = now + CACHE_TTL_SECONDS
        self._response_cache[url] = (expires_at, body)
        self._response_cache.move_to_end(url)
        self._prune_cache(now)

    def _prune_cache(self, now: float) -> None:
        for url, (expires_at, _) in list(self._response_cache.items()):
            if expires_at <= now:
                self._response_cache.pop(url, None)
        while len(self._response_cache) > MAX_CACHE_ENTRIES:
            self._response_cache.popitem(last=False)

    def _parse_feed_bytes(self, body: bytes, source: FeedSource, tag: str) -> list[FeedItem]:
        try:
            root = parse_xml(body)
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            raise WikidotApiError(f"RSS feed parse failed for {source.key}/{tag}: {exc}") from exc

        result: list[FeedItem] = []
        for entry in root.findall("./channel/item"):
            title = _find_text(entry, "title") or "Untitled"
            link = _find_text(entry, "link") or ""
            guid = _find_text(entry, "guid") or link or title
            published = _parse_rss_datetime(_find_text(entry, "pubDate"))
            author = _find_text(entry, "{http://purl.org/dc/elements/1.1/}creator")
            fullname = _fullname_from_link(link) or guid
            result.append(
                FeedItem(
                    source_key=source.key,
                    fullname=fullname,
                    title=title,
                    url=link,
                    created_by=author,
                    updated_at=published,
                    created_at=published,
                    tags=(tag,),
                )
            )
        return result

    def _parse_tag_page(self, body: bytes, source: FeedSource, tag: str) -> list[FeedItem]:
        parser = _TagPageLinkParser()
        parser.feed(body.decode("utf-8", errors="ignore"))
        result: list[FeedItem] = []
        seen: set[str] = set()
        for href, title in parser.links:
            link = _normalize_scp_link(href)
            fullname = _fullname_from_link(link)
            if not title or not link or not fullname or fullname in seen:
                continue
            if fullname.startswith(("system:", "nav:", "component:", "local--", "forum:")):
                continue
            seen.add(fullname)
            result.append(
                FeedItem(
                    source_key=source.key,
                    fullname=fullname,
                    title=title,
                    url=link,
                    tags=(tag,),
                )
            )
        return result


class _TagPageLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._in_page_content = False
        self._page_content_depth = 0
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_dict = dict(attrs)
        if tag == "div" and attrs_dict.get("id") == "page-content":
            self._in_page_content = True
            self._page_content_depth = 1
        elif self._in_page_content:
            self._page_content_depth += 1

        if self._in_page_content and tag == "a":
            self._current_href = attrs_dict.get("href")
            self._current_text = []

    def handle_endtag(self, tag: str):
        if self._in_page_content and tag == "a" and self._current_href:
            title = " ".join("".join(self._current_text).split())
            self.links.append((self._current_href, title))
            self._current_href = None
            self._current_text = []

        if self._in_page_content:
            self._page_content_depth -= 1
            if self._page_content_depth <= 0:
                self._in_page_content = False

    def handle_data(self, data: str):
        if self._in_page_content and self._current_href:
            self._current_text.append(data)


def _find_text(entry: ElementTree.Element, path: str) -> str | None:
    element = entry.find(path)
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _parse_rss_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _normalize_scp_link(href: str) -> str | None:
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    link = urljoin(SITE_BASE_URL + "/", href)
    parsed = urlparse(link)
    if parsed.netloc != "scp-wiki-cn.wikidot.com":
        return None
    return link.split("#", 1)[0]


def _fullname_from_link(link: str) -> str | None:
    if not link:
        return None
    path = urlparse(link).path.strip("/")
    return path or None


class _HtmlNode:
    def __init__(self, tag: str, attrs: dict[str, str] | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_HtmlNode] = []
        self.text_parts: list[str] = []

    def text(self) -> str:
        parts = [*self.text_parts]
        for child in self.children:
            parts.append(child.text())
        return " ".join(" ".join(parts).split())

    def has_class(self, class_name: str) -> bool:
        return class_name in self.attrs.get("class", "").split()

    def iter(self) -> list["_HtmlNode"]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.iter())
        return nodes

    def first(self, tag: str | None = None, class_name: str | None = None) -> "_HtmlNode | None":
        for node in self.iter():
            if tag and node.tag != tag:
                continue
            if class_name and not node.has_class(class_name):
                continue
            return node
        return None

    def find_all(self, tag: str | None = None, class_name: str | None = None) -> list["_HtmlNode"]:
        result = []
        for node in self.iter():
            if tag and node.tag != tag:
                continue
            if class_name and not node.has_class(class_name):
                continue
            result.append(node)
        return result


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        node = _HtmlNode(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str):
        if data.strip():
            self.stack[-1].text_parts.append(data)


def _parse_html_tree(body: bytes) -> _HtmlNode:
    parser = _TreeParser()
    parser.feed(body.decode("utf-8", errors="ignore"))
    return parser.root


def _extract_feature_panel(root: _HtmlNode, source: FeedSource, heading_text: str) -> FeedItem | None:
    for panel in root.find_all("div", "content-panel"):
        heading = panel.first("div", "panel-heading")
        if not heading or heading.text() != heading_text:
            continue

        title_node = panel.first("div", "feature-title")
        title_link = title_node.first("a") if title_node else None
        if not title_link:
            return None

        url = _normalize_scp_link(title_link.attrs.get("href", ""))
        fullname = _fullname_from_link(url or "")
        if not url or not fullname:
            return None

        subtitle = panel.first("div", "feature-subtitle")
        author = _extract_author_text(subtitle) if subtitle else None
        summary = "\n".join(node.text() for node in panel.find_all("em") if node.text())

        return FeedItem(
            source_key=source.key,
            fullname=fullname,
            title=title_link.text(),
            url=url,
            created_by=author,
            tags=(heading_text,),
            summary=summary or None,
        )
    return None


def _extract_homepage_contest(root: _HtmlNode, source: FeedSource) -> FeedItem | None:
    contest_banner = root.first("div", "summercontest")
    link_node = contest_banner.first("a") if contest_banner else None
    if not link_node:
        return None

    url = _normalize_scp_link(link_node.attrs.get("href", ""))
    fullname = _fullname_from_link(url or "")
    if not url or not fullname:
        return None

    summary_panel = _find_contest_summary_panel(root, contest_banner, url)
    summary = _paragraph_summary(summary_panel)
    title = _title_from_contest_url(fullname)

    return FeedItem(
        source_key=source.key,
        fullname=fullname,
        title=title,
        url=url,
        tags=("首页竞赛",),
        summary=summary or None,
    )


def _find_contest_summary_panel(
    root: _HtmlNode,
    contest_banner: _HtmlNode,
    contest_url: str,
) -> _HtmlNode | None:
    parent_children = _page_content_children(root)
    try:
        banner_index = parent_children.index(contest_banner)
        previous_siblings = parent_children[:banner_index]
    except ValueError:
        previous_siblings = []

    sibling_divs = [
        node
        for node in previous_siblings
        if node.tag == "div" and _paragraph_summary(node)
    ]

    for node in reversed(sibling_divs):
        if _node_contains_link(node, contest_url):
            return node

    for node in reversed(sibling_divs):
        if node.has_class("standalone") or node.has_class("content-panel"):
            return node

    nodes = root.iter()
    try:
        banner_tree_index = nodes.index(contest_banner)
    except ValueError:
        banner_tree_index = len(nodes)

    previous_divs = [
        node
        for node in nodes[:banner_tree_index]
        if node.tag == "div" and _paragraph_summary(node)
    ]

    for node in reversed(previous_divs):
        if _node_contains_link(node, contest_url):
            return node

    return previous_divs[-1] if previous_divs else None


def _page_content_children(root: _HtmlNode) -> list[_HtmlNode]:
    page_content = root.first("div", None)
    for node in root.find_all("div"):
        if node.attrs.get("id") == "page-content":
            return node.children
    return page_content.children if page_content else []


def _node_contains_link(node: _HtmlNode, target_url: str) -> bool:
    target_fullname = _fullname_from_link(target_url)
    for link in node.find_all("a"):
        link_url = _normalize_scp_link(link.attrs.get("href", ""))
        if _fullname_from_link(link_url or "") == target_fullname:
            return True
    return False


def _paragraph_summary(node: _HtmlNode | None) -> str:
    if not node:
        return ""
    paragraphs = [child.text() for child in node.children if child.tag == "p" and child.text()]
    if paragraphs:
        return "\n".join(paragraphs)
    return node.text()


def _extract_author_text(node: _HtmlNode) -> str | None:
    users = [
        user.text()
        for user in node.find_all("span", "printuser")
        if user.text()
    ]
    if users:
        return "、".join(users)

    value = node.text()
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.lower().startswith("by "):
        cleaned = cleaned[3:].strip()
    return cleaned or None


def _title_from_contest_url(fullname: str) -> str:
    title = fullname.replace("-", " ").strip()
    if not title:
        return "当前竞赛活动"
    return " ".join(part.capitalize() for part in title.split())
