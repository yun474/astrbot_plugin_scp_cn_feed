from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


SITE_NAME = "scp-wiki-cn"
SITE_BASE_URL = "https://scp-wiki-cn.wikidot.com"


@dataclass(frozen=True)
class FeedSource:
    key: str
    title: str
    description: str
    feed_tags: tuple[str, ...] = ()
    homepage_heading: str | None = None
    tags_any: tuple[str, ...] = ()
    tags_all: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    order: str = "created_at desc"


@dataclass(frozen=True)
class FeedItem:
    source_key: str
    fullname: str
    title: str
    url: str
    rating: int | None = None
    created_by: str | None = None
    updated_at: str | None = None
    created_at: str | None = None
    tags: tuple[str, ...] = ()
    summary: str | None = None
    summary_html: str | None = None
    image_url: str | None = None

    @property
    def item_id(self) -> str:
        return f"{self.source_key}:{self.fullname}"

    @property
    def sort_time(self) -> datetime:
        return _parse_wikidot_time(self.updated_at or self.created_at)


def page_url(fullname: str) -> str:
    return f"{SITE_BASE_URL}/{quote(fullname, safe=':-_')}"


def tag_feed_url(tag: str, title: str) -> str:
    encoded_tag = quote(tag, safe=",")
    encoded_title = quote(title.replace(" ", "+"), safe="+")
    return f"{SITE_BASE_URL}/feed/pages/tags/{encoded_tag}/t/{encoded_title}"


def tag_page_url(tag: str) -> str:
    return f"{SITE_BASE_URL}/system:page-tags/tag/{quote(tag, safe='')}"


def feed_item_from_meta(source_key: str, fullname: str, meta: dict[str, Any]) -> FeedItem:
    page_fullname = str(meta.get("fullname") or fullname)
    return FeedItem(
        source_key=source_key,
        fullname=page_fullname,
        title=str(meta.get("title") or page_fullname),
        url=page_url(page_fullname),
        rating=_to_int_or_none(meta.get("rating")),
        created_by=_to_str_or_none(meta.get("created_by")),
        updated_at=_to_str_or_none(meta.get("updated_at")),
        created_at=_to_str_or_none(meta.get("created_at")),
        tags=tuple(str(tag) for tag in meta.get("tags", []) if tag),
    )


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _parse_wikidot_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


SOURCES: dict[str, FeedSource] = {
    "featured_scp": FeedSource(
        key="featured_scp",
        title="精品 SCP",
        description="首页 精品原创SCP 模块，失败时回退 RSS/tag",
        feed_tags=("精品scp",),
        homepage_heading="精品原创SCP",
        tags_all=("精品scp",),
    ),
    "featured_tale": FeedSource(
        key="featured_tale",
        title="精品原创故事",
        description="首页 精品原创故事 模块，失败时回退 RSS/tag",
        feed_tags=("精品原创故事",),
        homepage_heading="精品原创故事",
        tags_all=("精品原创故事",),
    ),
    "contests": FeedSource(
        key="contests",
        title="竞赛与活动",
        description="首页竞赛横幅，失败时回退 RSS/tag",
        feed_tags=("竞赛", "征文", "活动", "比赛", "竞赛归档"),
        tags_any=("竞赛", "征文", "活动", "比赛", "竞赛归档"),
        pages=("contest-archive", "news", "site-news"),
        keywords=("竞赛", "征文", "活动", "优胜", "结果公布", "投稿", "比赛"),
        order="updated_at desc",
    ),
}


ALIASES: dict[str, str] = {
    "all": "all",
    "全部": "all",
    "精品": "featured_scp",
    "精品scp": "featured_scp",
    "scp": "featured_scp",
    "featured_scp": "featured_scp",
    "故事": "featured_tale",
    "精品故事": "featured_tale",
    "精品原创故事": "featured_tale",
    "tale": "featured_tale",
    "featured_tale": "featured_tale",
    "竞赛": "contests",
    "活动": "contests",
    "新闻": "contests",
    "contests": "contests",
}
