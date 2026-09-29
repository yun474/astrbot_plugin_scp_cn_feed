from __future__ import annotations

import json
import os
from pathlib import Path


class AnchorStore:
    """记录每个会话每个区块最后推送过的条目，用来判断有没有新内容。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._state = {}
        self._anchors: dict[str, dict[str, str]] = self._state.setdefault("latest_by_origin", {})

    def anchors(self, origin: str) -> dict[str, str]:
        return dict(self._anchors.get(origin, {}))

    def update(self, origin: str, anchors: dict[str, str]) -> None:
        if anchors:
            self._anchors.setdefault(origin, {}).update(anchors)
            self._save()

    def keep_only(self, origins: set[str]) -> None:
        stale = set(self._anchors) - origins
        for origin in stale:
            del self._anchors[origin]
        if stale:
            self._save()

    def pop_legacy_subscriptions(self) -> list[str]:
        """取出 0.4.x 及更早版本存在 state.json 里的订阅会话，只会返回一次。"""
        legacy = self._state.pop("subscriptions", None)
        for key in ("subscription_config_sync_version", "seen", "seen_by_origin"):
            self._state.pop(key, None)
        if legacy is None:
            return []
        self._save()
        return sorted(legacy) if isinstance(legacy, dict) else []

    def _save(self) -> None:
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        temp_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)
