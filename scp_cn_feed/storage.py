from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any


class FeedStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def subscribe(self, origin: str, source_key: str) -> None:
        with self._lock:
            subscriptions = self._state.setdefault("subscriptions", {})
            sources = set(subscriptions.get(origin, []))
            sources.add(source_key)
            subscriptions[origin] = sorted(sources)
            self.save()

    def unsubscribe(self, origin: str, source_key: str) -> None:
        with self._lock:
            subscriptions = self._state.setdefault("subscriptions", {})
            sources = set(subscriptions.get(origin, []))
            sources.discard(source_key)
            if sources:
                subscriptions[origin] = sorted(sources)
            else:
                subscriptions.pop(origin, None)
            self.save()

    def subscriptions_for(self, origin: str) -> set[str]:
        with self._lock:
            return set(self._state.get("subscriptions", {}).get(origin, []))

    def all_subscriptions(self) -> dict[str, set[str]]:
        with self._lock:
            return {
                origin: set(sources)
                for origin, sources in self._state.get("subscriptions", {}).items()
            }

    def mark_seen(self, origin: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        with self._lock:
            seen_by_origin = self._state.setdefault("seen_by_origin", {})
            seen = set(seen_by_origin.get(origin, []))
            seen.update(item_ids)
            seen_by_origin[origin] = sorted(seen)
            self.save()

    def is_seen(self, origin: str, item_id: str) -> bool:
        with self._lock:
            return item_id in set(self._state.get("seen_by_origin", {}).get(origin, []))

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    temp_file.write(payload)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, self.path)
                temp_path = None
            finally:
                if temp_path and temp_path.exists():
                    with suppress(OSError):
                        temp_path.unlink()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"subscriptions": {}, "seen_by_origin": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"subscriptions": {}, "seen_by_origin": {}}
        data.setdefault("subscriptions", {})
        data.setdefault("seen_by_origin", {})
        self._migrate_legacy_seen(data)
        return data

    def _migrate_legacy_seen(self, data: dict[str, Any]) -> None:
        legacy_seen = data.get("seen")
        seen_by_origin = data.get("seen_by_origin")
        if not isinstance(legacy_seen, list) or seen_by_origin:
            return

        subscriptions = data.get("subscriptions", {})
        if not isinstance(subscriptions, dict):
            return

        data["seen_by_origin"] = {
            origin: sorted(set(legacy_seen))
            for origin in subscriptions
        }
        data.pop("seen", None)
