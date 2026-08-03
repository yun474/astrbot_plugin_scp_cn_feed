from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .scp_cn_feed.models import ALIASES, SOURCES, FeedItem
from .scp_cn_feed.renderer import FeedRenderError, FeedRenderer, RenderOptions
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
PUSH_MODE_DAILY_REPORT = "daily_report"
PUSH_MODE_MODULE_SCREENSHOT = "module_screenshot"
PUSH_MODE_TEXT = "text"
PUSH_MODES = {
    PUSH_MODE_DAILY_REPORT,
    PUSH_MODE_MODULE_SCREENSHOT,
    PUSH_MODE_TEXT,
}


class ScpCnFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = FeedStore(self._state_path())
        self.service = FeedService(self.store, WikidotApiClient())
        self.renderer = FeedRenderer(self._render_dir(), self._render_options())
        self._task: asyncio.Task[None] | None = None
        self._initialize_subscription_config()

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

    def _render_dir(self) -> Path:
        return StarTools.get_data_dir(PLUGIN_NAME) / "renders"

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())

    @filter.command_group("scp")
    async def scp(self, event: AstrMessageEvent):
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        yield event.plain_result(self._help_text())

    @scp.command("帮助")
    async def help(self, event: AstrMessageEvent):
        """查看 SCP-CN Feed 插件帮助。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "SCP-CN Feed\n"
            "/scp 订阅\n"
            "/scp 取消订阅\n"
            "/scp 检查 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scp 截图 <精品scp|精品原创故事|竞赛>\n"
            "/scp 日报\n"
            "/scp 基线 <全部|精品scp|精品原创故事|竞赛>\n"
            "/scp 状态\n"
            "/scp 来源\n\n"
            "说明：优先解析首页模块，失败时回退 RSS/标签页。"
        )

    @scp.command("来源")
    async def sources(self, event: AstrMessageEvent):
        """查看内置信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        lines = ["当前内置信息源："]
        for source in SOURCES.values():
            lines.append(f"- {source.title}：{source.description}")
        yield event.plain_result("\n".join(lines))

    @scp.command("状态")
    async def status(self, event: AstrMessageEvent):
        """查看当前会话订阅状态。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        self._sync_subscriptions_from_config()
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

    @scp.command("订阅")
    async def subscribe(self, event: AstrMessageEvent):
        """订阅全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = set(SOURCE_ORDER)

        origin = event.unified_msg_origin
        self._sync_subscriptions_from_config()
        for source_key in source_keys:
            self.store.subscribe(origin, source_key)
        config_saved = self._sync_subscription_config_from_store()
        try:
            count = await self.service.baseline_sources(origin, source_keys)
        except WikidotApiError as exc:
            logger.warning(f"SCP-CN baseline failed: {exc}")
            config_note = "" if config_saved else "\n警告：写入插件配置失败，请检查 AstrBot 日志。"
            yield event.plain_result(
                "订阅已保存，但建立基线失败。下一次成功检查会先记录当前最新内容，避免推送历史内容。\n"
                f"错误：{exc}{config_note}"
            )
            return

        names = "、".join(SOURCES[key].title for key in sorted(source_keys))
        config_note = "\n订阅会话已同步到插件配置。" if config_saved else "\n警告：写入插件配置失败，请检查 AstrBot 日志。"
        yield event.plain_result(
            f"已订阅：{names}\n已抓取 {count} 条当前内容，并按来源记录最新内容作为基线，后续只推新增。"
            + config_note
        )

    @scp.command("取消订阅")
    async def unsubscribe(self, event: AstrMessageEvent):
        """取消订阅全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = set(SOURCE_ORDER)

        self._sync_subscriptions_from_config()
        for source_key in source_keys:
            self.store.unsubscribe(event.unified_msg_origin, source_key)
        config_saved = self._sync_subscription_config_from_store()

        names = "、".join(SOURCES[key].title for key in sorted(source_keys))
        config_note = "\n订阅会话已同步到插件配置。" if config_saved else "\n警告：写入插件配置失败，请检查 AstrBot 日志。"
        yield event.plain_result(f"已取消订阅：{names}{config_note}")

    @scp.command("基线")
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

    @scp.command("检查")
    async def check(self, event: AstrMessageEvent, source_name: str):
        """手动检查一个或全部 SCP-CN 信息源。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if not source_keys:
            yield event.plain_result(self._unknown_source_text(source_name))
            return

        fetched, fetch_errors = await self._fetch_sources_safely(
            source_keys,
            limit=MAX_ITEMS_PER_SOURCE,
            use_cache=False,
        )
        origin = event.unified_msg_origin
        messages: list[str] = []
        updates: list[tuple[str, list[FeedItem], list[FeedItem]]] = []
        for source_key, error in sorted(fetch_errors.items()):
            messages.append(f"{SOURCES[source_key].title}：检查失败：{error}")
        for source_key in sorted(fetched):
            items = fetched[source_key]
            new_items = self.service.only_new(origin, source_key, items)
            if not new_items:
                messages.append(f"{SOURCES[source_key].title}：暂无新增。")
                self.service.mark_latest(origin, source_key, items)
                continue
            updates.append((source_key, items, new_items))

        if not updates:
            if messages:
                yield event.plain_result("\n\n".join(messages))
            return

        push_mode = self._update_push_mode()
        if messages:
            yield event.plain_result("\n\n".join(messages))

        if push_mode == PUSH_MODE_DAILY_REPORT:
            sections, errors = await self._fetch_daily_sections(
                use_cache=False,
                seed=fetched,
                seed_errors=fetch_errors,
            )
            if self._daily_report_image_enabled():
                try:
                    self.renderer.prune_old_files()
                    image_path = await self.renderer.render_daily_report(
                        sections,
                        errors,
                        SOURCE_ORDER,
                        SOURCES,
                    )
                    yield event.image_result(str(image_path))
                except FeedRenderError as exc:
                    logger.warning(f"SCP-CN manual daily update render failed: {exc}")
                    yield event.plain_result(self._format_daily_report(sections, errors))
                except Exception as exc:
                    logger.warning(f"SCP-CN manual daily update render crashed: {exc}")
                    yield event.plain_result(self._format_daily_report(sections, errors))
            else:
                yield event.plain_result(self._format_daily_report(sections, errors))
        elif push_mode == PUSH_MODE_MODULE_SCREENSHOT:
            for source_key, _items, new_items in updates:
                try:
                    self.renderer.prune_old_files()
                    image_path = await self.renderer.render_update_screenshot(
                        SOURCES[source_key],
                        new_items,
                    )
                    yield event.plain_result(self._format_screenshot_notice(source_key, new_items))
                    yield event.image_result(str(image_path))
                except FeedRenderError as exc:
                    logger.warning(f"SCP-CN update screenshot failed for {source_key}: {exc}")
                    yield event.plain_result(self._format_push(SOURCES[source_key].title, new_items))
                except Exception as exc:
                    logger.warning(f"SCP-CN update screenshot crashed for {source_key}: {exc}")
                    yield event.plain_result(self._format_push(SOURCES[source_key].title, new_items))
        else:
            yield event.plain_result(
                "\n\n".join(
                    self._format_push(SOURCES[source_key].title, new_items)
                    for source_key, _items, new_items in updates
                )
            )

        for source_key, items, _new_items in updates:
            self.service.mark_latest(origin, source_key, items)

    @scp.command("截图")
    async def screenshot(self, event: AstrMessageEvent, source_name: str):
        """主动截取一个 SCP-CN 来源当前内容的网页区域。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        source_keys = self._resolve_sources(source_name)
        if len(source_keys) != 1:
            yield event.plain_result(
                f"截图只支持单个来源：{source_name}\n"
                "可用来源：精品scp、精品原创故事、竞赛"
            )
            return

        source_key = next(iter(source_keys))
        source = SOURCES[source_key]
        try:
            items = await self.service.fetch_source(
                source,
                limit=1,
                use_cache=False,
            )
        except WikidotApiError as exc:
            yield event.plain_result(f"截图失败，获取 {source.title} 内容失败：{exc}")
            return

        if not items:
            yield event.plain_result(f"截图失败，{source.title} 暂无可用内容。")
            return

        try:
            self.renderer.prune_old_files()
            image_path = await self.renderer.render_update_screenshot(source, items)
        except FeedRenderError as exc:
            logger.warning(f"SCP-CN manual screenshot failed for {source_key}: {exc}")
            yield event.plain_result(f"截图失败：{exc}")
            return
        except Exception as exc:
            logger.warning(f"SCP-CN manual screenshot crashed for {source_key}: {exc}")
            yield event.plain_result(f"截图失败：{exc}")
            return

        yield event.plain_result(self._format_screenshot_notice(source_key, items))
        yield event.image_result(str(image_path))

    @scp.command("日报")
    async def daily_report(self, event: AstrMessageEvent):
        """立即抓取并生成 SCP-CN 日报。"""
        if blocked_text := self._blocked_text(event):
            yield event.plain_result(blocked_text)
            return
        sections, errors = await self._fetch_daily_sections()

        if self._daily_report_image_enabled():
            try:
                self.renderer.prune_old_files()
                image_path = await self.renderer.render_daily_report(
                    sections,
                    errors,
                    SOURCE_ORDER,
                    SOURCES,
                )
                yield event.image_result(str(image_path))
                return
            except FeedRenderError as exc:
                logger.warning(f"SCP-CN daily report render failed: {exc}")
            except Exception as exc:
                logger.warning(f"SCP-CN daily report render crashed: {exc}")

        yield event.plain_result(self._format_daily_report(sections, errors))

    async def _fetch_daily_sections(
        self,
        *,
        use_cache: bool = True,
        seed: dict[str, list[FeedItem]] | None = None,
        seed_errors: dict[str, str] | None = None,
    ) -> tuple[dict[str, list[FeedItem]], dict[str, str]]:
        sections = {
            source_key: list(items)
            for source_key, items in (seed or {}).items()
            if source_key in SOURCE_ORDER
        }
        errors = {
            source_key: message
            for source_key, message in (seed_errors or {}).items()
            if source_key in SOURCE_ORDER
        }

        async def fetch_section(source_key: str) -> tuple[str, list[FeedItem], str | None]:
            try:
                items = await self.service.fetch_source(
                    SOURCES[source_key],
                    limit=DAILY_REPORT_ITEMS_PER_SOURCE,
                    use_cache=use_cache,
                )
                return source_key, items, None
            except WikidotApiError as exc:
                return source_key, [], str(exc)
            except Exception as exc:
                logger.warning(f"SCP-CN daily fetch crashed for {source_key}: {exc}")
                return source_key, [], str(exc)

        missing = [source_key for source_key in SOURCE_ORDER if source_key not in sections]
        results = await asyncio.gather(*(fetch_section(source_key) for source_key in missing))
        for source_key, items, error in results:
            sections[source_key] = items
            if error:
                errors[source_key] = error
            else:
                errors.pop(source_key, None)
        return sections, errors

    async def _fetch_sources_safely(
        self,
        source_keys: set[str],
        *,
        limit: int,
        use_cache: bool = True,
    ) -> tuple[dict[str, list[FeedItem]], dict[str, str]]:
        async def fetch_one(source_key: str) -> tuple[str, list[FeedItem], str | None]:
            try:
                items = await self.service.fetch_source(
                    SOURCES[source_key],
                    limit=limit,
                    use_cache=use_cache,
                )
                return source_key, items, None
            except WikidotApiError as exc:
                return source_key, [], str(exc)
            except Exception as exc:
                logger.warning(f"SCP-CN poll fetch crashed for {source_key}: {exc}")
                return source_key, [], str(exc)

        fetched: dict[str, list[FeedItem]] = {}
        errors: dict[str, str] = {}
        results = await asyncio.gather(*(fetch_one(key) for key in sorted(source_keys)))
        for source_key, items, error in results:
            if error:
                errors[source_key] = error
            else:
                fetched[source_key] = items
        return fetched, errors

    async def _daily_report_push_chain(
        self,
        sections: dict[str, list[FeedItem]],
        errors: dict[str, str],
    ) -> tuple[MessageChain, str, bool]:
        fallback_text = self._format_daily_report(sections, errors)
        if self._daily_report_image_enabled():
            try:
                self.renderer.prune_old_files()
                image_path = await self.renderer.render_daily_report(
                    sections,
                    errors,
                    SOURCE_ORDER,
                    SOURCES,
                )
                return MessageChain([Image.fromFileSystem(str(image_path))]), fallback_text, True
            except FeedRenderError as exc:
                logger.warning(f"SCP-CN automatic daily report render failed: {exc}")
            except Exception as exc:
                logger.warning(f"SCP-CN automatic daily report render crashed: {exc}")
        return MessageChain().message(fallback_text), fallback_text, False

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
        self._sync_subscriptions_from_config()
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

        push_mode = self._update_push_mode()
        source_keys = set().union(*subscriptions.values())
        if push_mode == PUSH_MODE_DAILY_REPORT:
            source_keys.update(SOURCE_ORDER)
        fetched, fetch_errors = await self._fetch_sources_safely(
            source_keys,
            limit=MAX_ITEMS_PER_SOURCE,
        )
        for source_key, error in fetch_errors.items():
            logger.warning(f"SCP-CN poll fetch failed for {source_key}: {error}")
        rendered_images: dict[tuple[str, tuple[str, ...]], Path] = {}
        daily_push: tuple[MessageChain, str, bool] | None = None

        for origin, subscribed_sources in subscriptions.items():
            latest_updates: list[tuple[str, list[FeedItem]]] = []
            for source_key in sorted(subscribed_sources):
                if source_key not in fetched:
                    continue
                items = fetched[source_key]
                new_items = self.service.only_new(origin, source_key, items)
                if not new_items:
                    self.service.mark_latest(origin, source_key, items)
                    continue
                latest_updates.append((source_key, items))

            if not latest_updates:
                continue

            fallback_text_parts = [
                self._format_push(
                    SOURCES[source_key].title,
                    self.service.only_new(origin, source_key, items),
                )
                for source_key, items in latest_updates
            ]
            fallback_text = "\n\n".join(fallback_text_parts)
            has_image = False

            if push_mode == PUSH_MODE_DAILY_REPORT:
                if daily_push is None:
                    sections = {
                        source_key: fetched.get(source_key, [])
                        for source_key in SOURCE_ORDER
                    }
                    daily_errors = {
                        source_key: message
                        for source_key, message in fetch_errors.items()
                        if source_key in SOURCE_ORDER
                    }
                    daily_push = await self._daily_report_push_chain(sections, daily_errors)
                message_chain, send_fallback_text, has_image = daily_push
            elif push_mode == PUSH_MODE_MODULE_SCREENSHOT:
                text_parts: list[str] = []
                image_components: list[Plain | Image] = []
                for source_key, items in latest_updates:
                    new_items = self.service.only_new(origin, source_key, items)
                    push_text = self._format_push(SOURCES[source_key].title, new_items)
                    try:
                        image_path = await self._render_cached_update_image(
                            rendered_images,
                            source_key,
                            new_items,
                        )
                        image_components.append(
                            Plain(
                                self._format_screenshot_notice(
                                    source_key,
                                    new_items,
                                ) + "\n"
                            )
                        )
                        image_components.append(Image.fromFileSystem(str(image_path)))
                    except FeedRenderError as exc:
                        logger.warning(f"SCP-CN update screenshot failed for {source_key}: {exc}")
                        text_parts.append(push_text)
                    except Exception as exc:
                        logger.warning(f"SCP-CN update screenshot crashed for {source_key}: {exc}")
                        text_parts.append(push_text)
                message_chain = self._build_push_message_chain(image_components, text_parts)
                send_fallback_text = fallback_text
                has_image = bool(image_components)
            else:
                message_chain = MessageChain().message(fallback_text)
                send_fallback_text = fallback_text

            try:
                await self.context.send_message(origin, message_chain)
            except Exception as exc:
                logger.warning(f"SCP-CN feed push failed for {origin}: {exc}")
                if not has_image:
                    continue
                try:
                    await self.context.send_message(
                        origin,
                        MessageChain().message(send_fallback_text),
                    )
                except Exception as fallback_exc:
                    logger.warning(f"SCP-CN feed text fallback failed for {origin}: {fallback_exc}")
                    continue

            for source_key, items in latest_updates:
                self.service.mark_latest(origin, source_key, items)
            await asyncio.sleep(PUSH_SEND_INTERVAL_SECONDS)

    async def _render_cached_update_image(
        self,
        cache: dict[tuple[str, tuple[str, ...]], Path],
        source_key: str,
        new_items: list[FeedItem],
    ) -> Path:
        cache_key = (source_key, tuple(item.item_id for item in new_items))
        cached = cache.get(cache_key)
        if cached and cached.exists():
            return cached

        self.renderer.prune_old_files()
        image_path = await self.renderer.render_update_screenshot(
            SOURCES[source_key],
            new_items,
        )
        cache[cache_key] = image_path
        return image_path

    def _build_push_message_chain(
        self,
        image_components: list[Plain | Image],
        text_parts: list[str],
    ) -> MessageChain:
        components: list[Plain | Image] = []
        components.extend(image_components)
        if text_parts:
            prefix = "\n\n" if components else ""
            components.append(Plain(prefix + "\n\n".join(text_parts)))
        if components:
            return MessageChain(components)
        return MessageChain()

    def _format_screenshot_notice(
        self,
        source_key: str,
        items: list[FeedItem],
    ) -> str:
        source = SOURCES[source_key]
        if not items:
            return f"SCP-CN {source.title}：暂无可截图内容。"

        blocks = []
        for item in items[:MAX_ITEMS_PER_SOURCE]:
            if source_key in {"featured_scp", "featured_tale"}:
                lines = [f"SCP-CN {source.title}：", item.title]
            else:
                lines = [f"SCP-CN {source.title}：{item.title}"]
            if source_key in {"featured_scp", "featured_tale"}:
                if item.summary:
                    lines.append(self._compact_summary(item))
                lines.append(f"原文：{item.url}")
            elif source_key == "contests":
                lines.append(f"竞赛链接：{item.url}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

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

    def _render_options(self) -> RenderOptions:
        return RenderOptions(
            browser_path=str(self.config.get("playwright_browser_path", "") or "").strip(),
            timeout_seconds=self._int_config("render_timeout_seconds", 35, minimum=5),
            retention_hours=self._int_config("render_retention_hours", 72, minimum=1),
        )

    def _initialize_subscription_config(self) -> None:
        configured = self._subscriptions_from_config()
        stored = self.store.all_subscriptions()

        if not self.store.subscription_config_sync_complete():
            # 首次升级时保留 state.json 中的旧订阅；配置中同会话的显式项优先。
            merged = {origin: set(sources) for origin, sources in stored.items()}
            merged.update(configured)
            self.store.replace_subscriptions(merged)
            if merged != configured:
                self._write_subscription_config(merged)
            return

        if configured != stored:
            self.store.replace_subscriptions(configured)

    def _sync_subscriptions_from_config(self) -> None:
        configured = self._subscriptions_from_config()
        if configured != self.store.all_subscriptions():
            self.store.replace_subscriptions(configured)

    def _sync_subscription_config_from_store(self) -> bool:
        return self._write_subscription_config(self.store.all_subscriptions())

    def _subscriptions_from_config(self) -> dict[str, set[str]]:
        raw_entries = self.config.get("subscription_sessions", [])
        if not isinstance(raw_entries, (list, tuple)):
            return {}

        subscriptions: dict[str, set[str]] = {}
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            origin = str(entry.get("origin", "") or "").strip()
            if not origin:
                continue
            sources = {
                source_key
                for source_key in SOURCE_ORDER
                if self._as_bool(entry.get(source_key, False))
            }
            if sources:
                subscriptions[origin] = sources
        return subscriptions

    def _write_subscription_config(self, subscriptions: dict[str, set[str]]) -> bool:
        entries = [
            {
                "__template_key": "subscription",
                "origin": origin,
                **{source_key: source_key in sources for source_key in SOURCE_ORDER},
            }
            for origin, sources in sorted(subscriptions.items())
            if origin and sources
        ]
        if self.config.get("subscription_sessions") == entries:
            return True

        self.config["subscription_sessions"] = entries
        save_config = getattr(self.config, "save_config", None)
        if not callable(save_config):
            logger.warning("SCP-CN subscription config cannot be persisted: save_config unavailable")
            return False
        try:
            save_config()
        except Exception as exc:
            logger.warning(f"SCP-CN subscription config save failed: {exc}")
            return False
        return True

    def _update_push_mode(self) -> str:
        value = str(self.config.get("update_push_mode", PUSH_MODE_MODULE_SCREENSHOT) or "").strip()
        if value in PUSH_MODES:
            return value
        return PUSH_MODE_MODULE_SCREENSHOT

    def _daily_report_image_enabled(self) -> bool:
        return self._bool_config("enable_daily_report_image", True)

    def _int_config(self, key: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _bool_config(self, key: str, default: bool) -> bool:
        return self._as_bool(self.config.get(key, default))

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", "关闭", "否"}
        return bool(value)

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
