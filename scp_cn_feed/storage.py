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
            self._cleanup_state()
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

    def replace_subscriptions(
        self,
        subscriptions: dict[str, set[str]],
        *,
        config_sync_complete: bool = True,
    ) -> None:
        with self._lock:
            self._state["subscriptions"] = {
                origin: sorted(sources)
                for origin, sources in sorted(subscriptions.items())
                if origin and sources
            }
            if config_sync_complete:
                self._state["subscription_config_sync_version"] = 1
            self._cleanup_state()
            self.save()

    def subscription_config_sync_complete(self) -> bool:
        with self._lock:
            return self._state.get("subscription_config_sync_version") == 1

    def latest_item_id(self, origin: str, source_key: str) -> str | None:
        with self._lock:
            value = self._state.get("latest_by_origin", {}).get(origin, {}).get(source_key)
            return str(value) if value else None

    def mark_latest(self, origin: str, source_key: str, item_id: str | None) -> None:
        if not item_id:
            return
        with self._lock:
            latest_by_origin = self._state.setdefault("latest_by_origin", {})
            latest_by_source = latest_by_origin.setdefault(origin, {})
            latest_by_source[source_key] = item_id
            self._cleanup_state()
            self.save()

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
            return {"subscriptions": {}, "latest_by_origin": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"subscriptions": {}, "latest_by_origin": {}}
        data.setdefault("subscriptions", {})
        data.setdefault("latest_by_origin", {})
        self._cleanup_loaded_state(data)
        return data

    def _cleanup_state(self) -> None:
        self._cleanup_loaded_state(self._state)

    def _cleanup_loaded_state(self, data: dict[str, Any]) -> None:
        subscriptions = data.setdefault("subscriptions", {})
        latest_by_origin = data.setdefault("latest_by_origin", {})

        if not isinstance(subscriptions, dict):
            data["subscriptions"] = {}
            subscriptions = data["subscriptions"]
        if not isinstance(latest_by_origin, dict):
            data["latest_by_origin"] = {}
            latest_by_origin = data["latest_by_origin"]

        for origin in list(latest_by_origin):
            subscribed_sources = set(subscriptions.get(origin, []))
            if not subscribed_sources:
                latest_by_origin.pop(origin, None)
                continue

            latest_by_source = latest_by_origin.get(origin)
            if not isinstance(latest_by_source, dict):
                latest_by_origin.pop(origin, None)
                continue

            for source_key in list(latest_by_source):
                if source_key not in subscribed_sources or not latest_by_source[source_key]:
                    latest_by_source.pop(source_key, None)

            if not latest_by_source:
                latest_by_origin.pop(origin, None)

        data.pop("seen", None)
        data.pop("seen_by_origin", None)
