from __future__ import annotations

from .models import FeedItem, FeedSource, SOURCES
from .storage import FeedStore
from .wikidot_api import WikidotApiClient


class FeedService:
    def __init__(self, store: FeedStore, client: WikidotApiClient):
        self.store = store
        self.client = client

    async def fetch_source(self, source: FeedSource, limit: int = 10) -> list[FeedItem]:
        return await self.client.fetch_source(source, limit=limit)

    async def fetch_many(self, source_keys: set[str], limit: int = 10) -> dict[str, list[FeedItem]]:
        result: dict[str, list[FeedItem]] = {}
        for key in source_keys:
            source = SOURCES[key]
            result[key] = await self.fetch_source(source, limit=limit)
        return result

    async def baseline_sources(self, origin: str, source_keys: set[str], limit: int = 30) -> int:
        total = 0
        for items in (await self.fetch_many(source_keys, limit=limit)).values():
            ids = [item.item_id for item in items]
            total += len(ids)
            self.store.mark_seen(origin, ids)
        return total

    def only_new(self, origin: str, items: list[FeedItem]) -> list[FeedItem]:
        return [item for item in items if not self.store.is_seen(origin, item.item_id)]
