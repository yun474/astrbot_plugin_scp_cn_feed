from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .scp_cn_feed.models import ALIASES, SOURCES, FeedItem
from .scp_cn_feed.service import FeedService
from .scp_cn_feed.storage import FeedStore
from .scp_cn_feed.wikidot_api import WikidotApiClient, WikidotApiError


DEFAULT_POLL_INTERVAL_DAYS = 1
MIN_POLL_INTERVAL_DAYS = 1
SECONDS_PER_DAY = 86400
MAX_ITEMS_PER_SOURCE = 5
DAILY_REPORT_ITEMS_PER_SOURCE = 5
PUSH_SEND_INTERVAL_SECONDS = 1.0
SOURCE_ORDER = ("featured_scp", "featured_tale", "contests")
PLUGIN_NAME = "astrbot_plugin_scp_cn_feed"


class ScpCnFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store = FeedStore(self._state_path())
        self.service = FeedService(self.store, WikidotApiClient())
        self._task: asyncio.Task[None] | None = None

    def _state_path(self) -> Path:
        plugin_data_path = StarTools.get_data_dir(PLUGIN_NAME)
        state_path = plugin_data_path / "state.json"
        legacy_state_path = Path(__file__).resolve().parent / "data" / "state.json"
        if not state_path.exists() and legacy_state_path.exists():
            temp_path = state_path.with_name(f".{state_path.name}.migrate.tmp")
            try:
                plugin_data_path.mkdir(parents=True, exist_ok=True)
                temp_path.write_bytes(legacy_state_path.read_bytes())
                os.replace(temp_path, state_path)
            except OSError as exc:
                logger.warning(f"SCP-CN feed state migration failed: {exc}")
            finally:
                if temp_path.exists():
                    with suppress(OSError):
                        temp_path.unlink()
        return state_path

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())

    @filter.command_group("scpfeed")
    async def scpfeed(self, event: AstrMessageEvent):
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        yield event.plain_result(self._help_text())

    @scpfeed.command("帮助")
    async def help(self, event: AstrMessageEvent):
        """查看 SCP-CN Feed 插件帮助。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "SCP-CN Feed\n"
            "/scpfeed 订阅 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scpfeed 取消 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scpfeed 检查 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scpfeed 日报\n"
            "/scpfeed 基线 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scpfeed 状态\n"
            "/scpfeed 来源\n\n"
            "说明：优先解析首页模块，失败时回退 RSS/标签页。"
        )

    @scpfeed.command("来源")
    async def sources(self, event: AstrMessageEvent):
        """查看内置信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        lines = ["当前内置信息源："]
        for source in SOURCES.values():
            lines.append(f"- {source.title}：{source.description}")
        yield event.plain_result("\n".join(lines))

    @scpfeed.command("状态")
    async def status(self, event: AstrMessageEvent):
        """查看当前会话订阅状态。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        origin = event.unified_msg_origin
        subscriptions = self.store.subscriptions_for(origin)
        interval_days = self._poll_interval_days()
        if not subscriptions:
            yield event.plain_result(
                f"当前会话还没有订阅。\n会话标识：{origin}\n轮询间隔：{interval_days} 天"
            )
            return
        names = [SOURCES[key].title for key in sorted(subscriptions)]
        yield event.plain_result(
            "当前会话已订阅："
            + "、".join(names)
            + f"\n会话标识：{origin}\n轮询间隔：{interval_days} 天"
        )

    @scpfeed.command("订阅")
    async def subscribe(self, event: AstrMessageEvent, source_name: str):
        """订阅一个或全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if not source_keys:
            yield event.plain_result(self._unknown_source_text(source_name))
            return

        for source_key in source_keys:
            self.store.subscribe(event.unified_msg_origin, source_key)

        origin = event.unified_msg_origin
        try:
            count = await self.service.baseline_sources(origin, source_keys)
        except WikidotApiError as exc:
            logger.warning(f"SCP-CN baseline failed: {exc}")
            yield event.plain_result(
                "订阅已保存，但建立基线失败。下一次成功检查会先记录当前最新内容，避免推送历史内容。\n"
                f"错误：{exc}"
            )
            return

        names = "、".join(SOURCES[key].title for key in sorted(source_keys))
        yield event.plain_result(f"已订阅：{names}\n已抓取 {count} 条当前内容，并按来源记录最新内容作为基线，后续只推新增。")

    @scpfeed.command("取消")
    async def unsubscribe(self, event: AstrMessageEvent, source_name: str):
        """取消订阅一个或全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if not source_keys:
            yield event.plain_result(self._unknown_source_text(source_name))
            return

        for source_key in source_keys:
            self.store.unsubscribe(event.unified_msg_origin, source_key)

        names = "、".join(SOURCES[key].title for key in sorted(source_keys))
        yield event.plain_result(f"已取消订阅：{names}")

    @scpfeed.command("基线")
    async def baseline(self, event: AstrMessageEvent, source_name: str):
        """手动把当前源内容标记为已读，避免推送历史内容。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if not source_keys:
            yield event.plain_result(self._unknown_source_text(source_name))
            return

        try:
            count = await self.service.baseline_sources(event.unified_msg_origin, source_keys)
        except WikidotApiError as exc:
            yield event.plain_result(f"建立基线失败：{exc}")
            return

        yield event.plain_result(f"已抓取 {count} 条当前内容，并按来源记录最新内容作为基线。")

    @scpfeed.command("检查")
    async def check(self, event: AstrMessageEvent, source_name: str):
        """手动检查一个或全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if not source_keys:
            yield event.plain_result(self._unknown_source_text(source_name))
            return

        try:
            fetched = await self.service.fetch_many(
                source_keys,
                limit=MAX_ITEMS_PER_SOURCE,
                use_cache=False,
            )
        except WikidotApiError as exc:
            yield event.plain_result(f"检查失败：{exc}")
            return

        origin = event.unified_msg_origin
        messages: list[str] = []
        for source_key in sorted(fetched):
            items = fetched[source_key]
            new_items = self.service.only_new(origin, source_key, items)
            if not new_items:
                messages.append(f"{SOURCES[source_key].title}：暂无新增。")
                self.service.mark_latest(origin, source_key, items)
                continue
            messages.append(self._format_push(SOURCES[source_key].title, new_items))
            self.service.mark_latest(origin, source_key, items)

        yield event.plain_result("\n\n".join(messages))

    @scpfeed.command("日报")
    async def daily_report(self, event: AstrMessageEvent):
        """立即抓取并生成 SCP-CN 日报。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        sections: dict[str, list[FeedItem]] = {}
        errors: dict[str, str] = {}

        for source_key in SOURCE_ORDER:
            try:
                sections[source_key] = await self.service.fetch_source(
                    SOURCES[source_key],
                    limit=DAILY_REPORT_ITEMS_PER_SOURCE,
                )
            except WikidotApiError as exc:
                sections[source_key] = []
                errors[source_key] = str(exc)

        yield event.plain_result(self._format_daily_report(sections, errors))

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.service.close()

    async def _poll_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"SCP-CN feed poll failed: {exc}")
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self):
        subscriptions = self.store.all_subscriptions()
        if not subscriptions:
            return
        subscriptions = {
            origin: sources
            for origin, sources in subscriptions.items()
            if self._is_origin_allowed(origin)
        }
        if not subscriptions:
            return

        source_keys = set().union(*subscriptions.values())
        fetched = await self.service.fetch_many(source_keys, limit=MAX_ITEMS_PER_SOURCE)

        for origin, subscribed_sources in subscriptions.items():
            parts: list[str] = []
            latest_updates: list[tuple[str, list[FeedItem]]] = []
            for source_key in sorted(subscribed_sources):
                items = fetched.get(source_key, [])
                new_items = self.service.only_new(origin, source_key, items)
                if not new_items:
                    self.service.mark_latest(origin, source_key, items)
                    continue
                parts.append(self._format_push(SOURCES[source_key].title, new_items))
                latest_updates.append((source_key, items))
            if parts:
                try:
                    await self.context.send_message(origin, MessageChain().message("\n\n".join(parts)))
                except Exception as exc:
                    logger.warning(f"SCP-CN feed push failed for {origin}: {exc}")
                else:
                    for source_key, items in latest_updates:
                        self.service.mark_latest(origin, source_key, items)
                await asyncio.sleep(PUSH_SEND_INTERVAL_SECONDS)

    def _resolve_sources(self, source_name: str) -> set[str]:
        source_key = ALIASES.get(source_name.strip().lower())
        if source_key == "all":
            return set(SOURCES)
        if source_key in SOURCES:
            return {source_key}
        return set()

    def _poll_interval_days(self) -> int:
        try:
            configured = int(self.config.get("poll_interval_days", DEFAULT_POLL_INTERVAL_DAYS))
        except (TypeError, ValueError):
            configured = DEFAULT_POLL_INTERVAL_DAYS
        return max(MIN_POLL_INTERVAL_DAYS, configured)

    def _poll_interval_seconds(self) -> int:
        return self._poll_interval_days() * SECONDS_PER_DAY

    def _blocked_text(self, event: AstrMessageEvent) -> str | None:
        origin = event.unified_msg_origin
        if self._is_origin_allowed(origin):
            return None
        return (
            "当前会话未启用 SCP-CN Feed。\n"
            f"当前会话标识：{origin}\n"
            "请在插件配置 whitelist_origins / blacklist_origins 中调整。"
        )

    def _is_origin_allowed(self, origin: str) -> bool:
        blacklist = self._configured_origin_set("blacklist_origins")
        if origin in blacklist:
            return False

        whitelist = self._configured_origin_set("whitelist_origins")
        if whitelist and origin not in whitelist:
            return False

        return True

    def _configured_origin_set(self, key: str) -> set[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            raw_items = value.replace(",", "\n").splitlines()
        elif isinstance(value, (list, tuple, set)):
            raw_items = value
        else:
            raw_items = []
        return {
            str(item).strip()
            for item in raw_items
            if str(item).strip()
        }

    def _unknown_source_text(self, source_name: str) -> str:
        return (
            f"未知来源：{source_name}\n"
            "可用来源：全部、精品scp、精品原创故事、竞赛"
        )

    def _format_push(self, source_title: str, items: list[FeedItem]) -> str:
        lines = [f"[SCP-CN {source_title}更新]"]
        for item in items[:MAX_ITEMS_PER_SOURCE]:
            rating = f"评分：{item.rating}" if item.rating is not None else "评分：未知"
            author = f"作者：{item.created_by}" if item.created_by else "作者：未知"
            block = [f"标题：{item.title}", author, rating, f"链接：{item.url}"]
            if item.summary:
                block.append(f"摘要：{self._compact_summary(item)}")
            lines.append("\n".join(block))
        return "\n\n".join(lines)

    def _format_daily_report(
        self,
        sections: dict[str, list[FeedItem]],
        errors: dict[str, str],
    ) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"SCP-CN 日报 {today}", "数据源：首页模块，失败时回退 RSS/标签页"]

        for source_key in SOURCE_ORDER:
            source = SOURCES[source_key]
            lines.append("")
            lines.append(f"【{source.title}】")

            items = sections.get(source_key, [])
            if not items:
                if source_key in errors:
                    lines.append(f"获取失败：{errors[source_key]}")
                else:
                    lines.append("暂无可用内容。")
                continue

            for index, item in enumerate(items[:DAILY_REPORT_ITEMS_PER_SOURCE], start=1):
                author = f" 作者：{item.created_by}" if item.created_by else ""
                rating = f" 评分：{item.rating}" if item.rating is not None else ""
                lines.append(f"{index}. {item.title}{author}{rating}")
                lines.append(f"   {item.url}")
                if item.summary:
                    lines.append(f"   {self._compact_summary(item)}")

        return "\n".join(lines)

    def _compact_summary(self, item: FeedItem) -> str:
        limit = 320 if item.source_key == "contests" else 140
        compact = " ".join((item.summary or "").split())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."
