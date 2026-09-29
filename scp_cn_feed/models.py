from __future__ import annotations

from dataclasses import dataclass


SITE_BASE_URL = "https://scp-wiki-cn.wikidot.com"


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    en: str
    icon: str


@dataclass(frozen=True)
class FeedItem:
    section: str
    uid: str
    title: str
    url: str
    author: str | None = None
    summary: str | None = None
    summary_html: str | None = None
    tag: str | None = None
    image_url: str | None = None

    @property
    def item_id(self) -> str:
        return f"{self.section}:{self.uid}"


SECTIONS: dict[str, Section] = {
    section.key: section
    for section in (
        Section("featured_scp", "精品原创 SCP", "FEATURED SCP", "☣️"),
        Section("featured_tale", "精品原创故事", "FEATURED TALE", "📖"),
        Section("featured_library", "精品原创图书馆", "WANDERERS' LIBRARY", "📚"),
        Section("featured_theme", "主题精品", "THEMED FEATURE", "🎭"),
        Section("contests", "竞赛与活动", "CONTESTS", "🏆"),
    )
}
