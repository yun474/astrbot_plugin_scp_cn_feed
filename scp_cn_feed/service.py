from __future__ import annotations

from .models import FeedItem, FeedSource, SOURCES
from .storage import FeedStore
from .wikidot_api import WikidotApiClient


class FeedService:
    def __init__(self, store: FeedStore, client: WikidotApiClient):
        self.store = store
        self.client = client

    async def fetch_source(
        self,
        source: FeedSource,
        limit: int = 10,
        use_cache: bool = True,
    ) -> list[FeedItem]:
        return await self.client.fetch_source(source, limit=limit, use_cache=use_cache)

    async def fetch_many(
        self,
        source_keys: set[str],
        limit: int = 10,
        use_cache: bool = True,
    ) -> dict[str, list[FeedItem]]:
        result: dict[str, list[FeedItem]] = {}
        for key in source_keys:
            source = SOURCES[key]
            result[key] = await self.fetch_source(source, limit=limit, use_cache=use_cache)
        return result

    async def baseline_sources(self, origin: str, source_keys: set[str], limit: int = 30) -> int:
        total = 0
        for source_key, items in (await self.fetch_many(source_keys, limit=limit)).items():
            total += len(items)
            self.mark_latest(origin, source_key, items)
        return total

    def only_new(self, origin: str, source_key: str, items: list[FeedItem]) -> list[FeedItem]:
        latest_item_id = self.store.latest_item_id(origin, source_key)
        if not latest_item_id:
            return []

        new_items: list[FeedItem] = []
        for item in items:
            if item.item_id == latest_item_id:
                break
            new_items.append(item)
        return new_items

    def mark_latest(self, origin: str, source_key: str, items: list[FeedItem]) -> None:
        if items:
            self.store.mark_latest(origin, source_key, items[0].item_id)
