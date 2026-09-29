from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

from .scp_cn_feed.fetcher import FetchError, HomepageFetcher
from .scp_cn_feed.messages import (
    HELP_TEXT,
    build_keyboard,
    format_markdown,
    format_markdown_text,
    format_text,
)
from .scp_cn_feed.models import SECTIONS, FeedItem
from .scp_cn_feed.qq_official import QQOfficialTarget
from .scp_cn_feed.renderer import CardRenderer
from .scp_cn_feed.store import AnchorStore


PLUGIN_NAME = "astrbot_plugin_scp_cn_feed"
MODE_LABELS = {"image": "图片卡片", "markdown": "QQ 官方 Markdown + 按钮", "text": "纯文字"}
FIRST_POLL_DELAY_SECONDS = 30
PUSH_INTERVAL_SECONDS = 1.0


class ScpCnFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = AnchorStore(data_dir / "state.json")
        self.fetcher = HomepageFetcher()
        self.renderer = CardRenderer(
            data_dir / "renders",
            str(config.get("playwright_browser_path", "") or ""),
        )
        self._task: asyncio.Task | None = None
        if legacy := self.store.pop_legacy_subscriptions():
            self._set_sessions(self._sessions() + legacy)

    async def initialize(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        if self._task:
            self._task.cancel()
        await self.fetcher.close()

    @filter.command("scp")
    async def scp(self, event: AstrMessageEvent, action: str = ""):
        """SCP-CN 中文站推送：/scp [日报|订阅|退订|状态|帮助]"""
        action = action.strip()
        if action in ("", "日报"):
            async for result in self._reply_report(event):
                yield result
            return

        origin = event.unified_msg_origin
        if action == "订阅":
            reply = await self._subscribe(origin)
        elif action == "退订":
            reply = self._unsubscribe(origin)
        elif action == "状态":
            reply = self._status(origin)
        else:
            reply = HELP_TEXT
        keyboard = build_keyboard(links=self._markdown_links())
        if not await self._reply_markdown(event, format_markdown_text(reply), keyboard):
            yield event.plain_result(reply)

    # ---------- 指令 ----------

    async def _reply_report(self, event: AstrMessageEvent):
        try:
            report = await self.fetcher.fetch()
        except FetchError as exc:
            yield event.plain_result(f"中文站没抓下来：{exc}")
            return

        items = [report[key] for key in self._sections() if key in report]
        links = self._markdown_links()
        markdown = format_markdown(items, update=False, links=links)
        if await self._reply_markdown(event, markdown, build_keyboard(links=links)):
            return
        if image := await self._render(items, update=False):
            yield event.image_result(str(image))
        else:
            yield event.plain_result(format_text(items, update=False))

    async def _subscribe(self, origin: str) -> str:
        sections = "、".join(SECTIONS[key].title for key in self._sections())
        sessions = self._sessions()
        if origin in sessions:
            return f"本会话早就订阅啦\n推送区块：{sections}"

        self._set_sessions(sessions + [origin])
        try:
            # 订阅时把首页当前内容记为已读，之后只推新的。
            report = await self.fetcher.fetch()
            self.store.update(origin, {key: item.item_id for key, item in report.items()})
        except FetchError as exc:
            logger.warning(f"[SCP-CN] 订阅时记录当前内容失败，下次检查时再记：{exc}")
        return f"订阅成功，首页有新内容会推到这里\n推送区块：{sections}"

    def _unsubscribe(self, origin: str) -> str:
        sessions = self._sessions()
        if origin not in sessions:
            return "本会话还没订阅哦"
        remaining = [session for session in sessions if session != origin]
        self._set_sessions(remaining)
        self.store.keep_only(set(remaining))
        return "已退订，不会再往这里推送了"

    def _status(self, origin: str) -> str:
        subscribed = "已订阅" if origin in self._sessions() else "未订阅"
        return (
            f"SCP-CN 推送状态：{subscribed}\n"
            f"推送区块：{'、'.join(SECTIONS[key].title for key in self._sections())}\n"
            f"推送形式：{MODE_LABELS[self._mode()]}，每 {self._interval_days()} 天检查一次\n"
            f"会话 ID：{origin}"
        )

    async def _reply_markdown(self, event: AstrMessageEvent, content: str, keyboard: dict) -> bool:
        """Markdown 模式下给 QQ 官方会话被动回复，成功返回 True。"""
        if self._mode() != "markdown":
            return False
        origin = event.unified_msg_origin
        target = QQOfficialTarget.resolve(self._platform(origin), origin, event.message_obj.message_id)
        if not target:
            return False
        try:
            await target.send_markdown(content, keyboard)
        except Exception as exc:
            logger.warning(f"[SCP-CN] Markdown 回复失败，改用普通消息：{exc}")
            return False
        event.stop_event()
        return True

    # ---------- 定时推送 ----------

    async def _poll_loop(self):
        await asyncio.sleep(FIRST_POLL_DELAY_SECONDS)
        while True:
            try:
                await self.check_updates()
            except Exception as exc:
                logger.warning(f"[SCP-CN] 检查更新失败：{exc}")
            await asyncio.sleep(self._interval_days() * 86400)

    async def check_updates(self) -> None:
        sessions = self._sessions()
        self.store.keep_only(set(sessions))
        if not sessions:
            return

        report = await self.fetcher.fetch(fresh=True)
        current = {key: report[key] for key in self._sections() if key in report}
        for origin in sessions:
            anchors = self.store.anchors(origin)
            fresh = [item for key, item in current.items() if key in anchors and anchors[key] != item.item_id]
            # 新勾选的区块或新会话没有记录，先记下当前内容，不推旧内容。
            self.store.update(origin, {key: item.item_id for key, item in current.items() if key not in anchors})
            if not fresh:
                continue
            try:
                sent = await self._push(origin, fresh)
            except Exception as exc:
                logger.warning(f"[SCP-CN] 推送到 {origin} 失败，下次检查会重试：{exc}")
                continue
            if sent:
                self.store.update(origin, {item.section: item.item_id for item in fresh})
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)

    async def _push(self, origin: str, items: list[FeedItem]) -> bool:
        mode = self._mode()
        target = QQOfficialTarget.resolve(self._platform(origin), origin)
        if target and mode == "markdown":
            links = self._markdown_links()
            try:
                await target.send_markdown(
                    format_markdown(items, update=True, links=links),
                    build_keyboard(links=links),
                )
                return True
            except Exception as exc:
                logger.warning(f"[SCP-CN] Markdown 推送失败，改发图片卡片：{exc}")

        image = await self._render(items, update=True) if mode != "text" else None
        if target:
            if image:
                await target.send_image(image)
            else:
                await target.send_markdown(format_text(items, update=True))
            return True

        chain = MessageChain([Image.fromFileSystem(str(image))]) if image else MessageChain().message(
            format_text(items, update=True)
        )
        return bool(await self.context.send_message(origin, chain))

    async def _render(self, items: list[FeedItem], *, update: bool) -> Path | None:
        try:
            return await self.renderer.render(items, update=update)
        except Exception as exc:
            logger.warning(f"[SCP-CN] 卡片渲染失败，改发文字：{exc}")
            return None

    # ---------- 配置 ----------

    def _platform(self, origin: str) -> Any:
        platform_id = origin.split(":", 1)[0]
        return next(
            (p for p in self.context.platform_manager.platform_insts if p.meta().id == platform_id),
            None,
        )

    def _sessions(self) -> list[str]:
        return [str(s).strip() for s in self.config.get("subscribed_sessions", []) if str(s).strip()]

    def _set_sessions(self, sessions: list[str]) -> None:
        self.config["subscribed_sessions"] = list(dict.fromkeys(sessions))
        self.config.save_config()

    def _sections(self) -> list[str]:
        chosen = set(self.config.get("push_sections") or SECTIONS)
        return [key for key in SECTIONS if key in chosen]

    def _mode(self) -> str:
        mode = self.config.get("push_mode", "image")
        return mode if mode in MODE_LABELS else "image"

    def _markdown_links(self) -> bool:
        return bool(self.config.get("markdown_links", True))

    def _interval_days(self) -> int:
        return max(1, int(self.config.get("poll_interval_days", 1) or 1))
