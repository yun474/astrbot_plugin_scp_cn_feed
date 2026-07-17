from __future__ import annotations

import html
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import FeedItem, FeedSource, SITE_BASE_URL


DEFAULT_VIEWPORT_WIDTH = 980
DEFAULT_DAILY_HEIGHT = 1280
DEFAULT_UPDATE_HEIGHT = 900
DEFAULT_TIMEOUT_SECONDS = 35
DEFAULT_RETENTION_HOURS = 72
SCP_FOUNDATION_LOGO_URL = (
    "https://scp-wiki-cn.wikidot.com/local--files/component:theme/logo.png"
)


class FeedRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderOptions:
    browser_path: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    retention_hours: int = DEFAULT_RETENTION_HOURS

    @property
    def timeout_ms(self) -> int:
        return max(5, self.timeout_seconds) * 1000


class FeedRenderer:
    def __init__(self, output_dir: Path, options: RenderOptions | None = None):
        self.output_dir = output_dir
        self.options = options or RenderOptions()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def render_daily_report(
        self,
        sections: dict[str, list[FeedItem]],
        errors: dict[str, str],
        source_order: tuple[str, ...],
        sources: dict[str, FeedSource],
    ) -> Path:
        html_text = self._build_daily_html(sections, errors, source_order, sources)
        output_path = self._new_output_path("daily")

        async with await self._playwright() as p:
            browser = await self._launch_browser(p)
            try:
                page = await browser.new_page(
                    viewport={"width": DEFAULT_VIEWPORT_WIDTH, "height": DEFAULT_DAILY_HEIGHT},
                    device_scale_factor=1,
                )
                await page.set_content(html_text, wait_until="load", timeout=self.options.timeout_ms)
                await self._wait_for_images(page)
                await page.locator(".report").screenshot(
                    path=str(output_path),
                    timeout=self.options.timeout_ms,
                )
            finally:
                await browser.close()

        return output_path

    async def render_update_screenshot(
        self,
        source: FeedSource,
        items: list[FeedItem],
    ) -> Path:
        if not items:
            raise FeedRenderError("没有可截图的更新条目")

        async with await self._playwright() as p:
            browser = await self._launch_browser(p)
            try:
                homepage_path = await self._try_homepage_region_screenshot(browser, source, items)
                if homepage_path:
                    return homepage_path
                return await self._screenshot_item_page(browser, items[0])
            finally:
                await browser.close()

    def prune_old_files(self) -> None:
        max_age_seconds = max(1, self.options.retention_hours) * 3600
        cutoff = time.time() - max_age_seconds
        for path in self.output_dir.glob("scp_cn_feed_*.png"):
            with suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    async def _try_homepage_region_screenshot(
        self,
        browser: Any,
        source: FeedSource,
        items: list[FeedItem],
    ) -> Path | None:
        if not source.homepage_heading and source.key != "contests":
            return None

        page = await browser.new_page(
            viewport={"width": 1200, "height": DEFAULT_UPDATE_HEIGHT},
            device_scale_factor=1,
        )
        try:
            await page.goto(
                SITE_BASE_URL + "/",
                wait_until="domcontentloaded",
                timeout=self.options.timeout_ms,
            )
            await self._settle_page(page)
            await self._hide_noisy_page_parts(page)
            await self._wait_for_images(page)

            if source.key == "contests":
                return await self._try_contest_homepage_screenshot(page, source, items)

            locator = self._homepage_region_locator(page, source)
            if await locator.count() == 0:
                return None

            target = locator.first
            if source.key != "contests" and not await self._region_matches_items(target, items):
                return None

            output_path = self._new_output_path(f"update_{source.key}_home")
            await target.screenshot(path=str(output_path), timeout=self.options.timeout_ms)
            return output_path
        finally:
            await page.close()

    async def _try_contest_homepage_screenshot(
        self,
        page: Any,
        source: FeedSource,
        items: list[FeedItem],
    ) -> Path | None:
        if not items:
            return None

        banner = page.locator("div.summercontest")
        if await banner.count() == 0:
            return None

        with suppress(Exception):
            await banner.first.scroll_into_view_if_needed(timeout=5000)
            await page.wait_for_timeout(800)

        clip = await self._contest_homepage_clip(page, items[0])
        if not clip:
            return None

        output_path = self._new_output_path(f"update_{source.key}_home")
        await page.screenshot(path=str(output_path), clip=clip, timeout=self.options.timeout_ms)
        return output_path

    async def _contest_homepage_clip(self, page: Any, item: FeedItem) -> dict[str, float] | None:
        return await page.evaluate(
            """(itemUrl) => {
                const pageContent = document.querySelector("#page-content") || document.body;
                const banner = pageContent.querySelector("div.summercontest") || document.querySelector("div.summercontest");
                if (!banner) return null;

                const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const isVisible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 1 && rect.height > 1 && style.display !== "none" && style.visibility !== "hidden";
                };
                const sameTarget = (href) => {
                    try {
                        const target = new URL(itemUrl, window.location.href).pathname.replace(/\\/$/, "");
                        const current = new URL(href, window.location.href).pathname.replace(/\\/$/, "");
                        return target && current === target;
                    } catch {
                        return false;
                    }
                };
                const containsTargetLink = (element) => {
                    return Array.from(element.querySelectorAll("a[href]")).some((link) => sameTarget(link.href));
                };
                const rectFor = (element) => {
                    const rect = element.getBoundingClientRect();
                    return {
                        left: rect.left + window.scrollX,
                        top: rect.top + window.scrollY,
                        right: rect.right + window.scrollX,
                        bottom: rect.bottom + window.scrollY,
                        width: rect.width,
                        height: rect.height,
                    };
                };

                const children = Array.from(pageContent.children).filter((element) => {
                    return !["SCRIPT", "STYLE", "LINK", "META"].includes(element.tagName) && isVisible(element);
                });
                let bannerIndex = children.findIndex((element) => element === banner || element.contains(banner));
                if (bannerIndex < 0) bannerIndex = children.length;

                const previous = children.slice(0, bannerIndex).filter((element) => {
                    return normalize(element.innerText).length > 0 || element.querySelector("img, a, div");
                });
                let summary = previous.slice().reverse().find((element) => containsTargetLink(element));
                if (!summary) {
                    summary = previous.slice().reverse().find((element) => {
                        return element.classList.contains("standalone") || element.classList.contains("content-panel");
                    });
                }

                const next = children.slice(Math.min(bannerIndex + 1, children.length)).find((element) => {
                    if (element === banner || banner.contains(element)) return false;
                    const rect = element.getBoundingClientRect();
                    return rect.height > 8 && (normalize(element.innerText) || element.querySelector("img, a, div"));
                });

                const bannerRect = rectFor(banner);
                const summaryRect = summary ? rectFor(summary) : bannerRect;
                const contentRect = rectFor(pageContent);
                const nextRect = next ? rectFor(next) : null;

                const top = Math.max(0, Math.min(summaryRect.top, bannerRect.top) - 12);
                const left = Math.max(0, Math.min(summaryRect.left, bannerRect.left, contentRect.left) - 8);
                const right = Math.min(
                    document.documentElement.scrollWidth,
                    Math.max(summaryRect.right, bannerRect.right, contentRect.right) + 8
                );
                const naturalBottom = Math.max(summaryRect.bottom, bannerRect.bottom) + 12;
                const bottom = nextRect ? Math.max(naturalBottom, nextRect.top) : naturalBottom;

                return {
                    x: left,
                    y: top,
                    width: Math.max(1, right - left),
                    height: Math.max(1, bottom - top),
                };
            }""",
            item.url,
        )

    async def _screenshot_item_page(self, browser: Any, item: FeedItem) -> Path:
        page = await browser.new_page(
            viewport={"width": 1200, "height": DEFAULT_UPDATE_HEIGHT},
            device_scale_factor=1,
        )
        try:
            await page.goto(item.url, wait_until="domcontentloaded", timeout=self.options.timeout_ms)
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=min(8000, self.options.timeout_ms))
            await self._settle_page(page)
            await self._hide_noisy_page_parts(page)
            await self._wait_for_images(page)

            content = page.locator("#page-content")
            target = content.first if await content.count() else page.locator("body").first
            box = await target.bounding_box(timeout=self.options.timeout_ms)
            if not box:
                raise FeedRenderError(f"页面区域不可截图：{item.url}")

            output_path = self._new_output_path(f"update_{item.source_key}_page")
            await page.screenshot(
                path=str(output_path),
                clip={
                    "x": max(0, box["x"]),
                    "y": max(0, box["y"]),
                    "width": min(max(1, box["width"]), 1120),
                    "height": min(max(1, box["height"]), DEFAULT_UPDATE_HEIGHT),
                },
                timeout=self.options.timeout_ms,
            )
            return output_path
        finally:
            await page.close()

    async def _playwright(self) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise FeedRenderError("未安装 playwright，无法渲染图片") from exc
        return async_playwright()

    async def _launch_browser(self, p: Any) -> Any:
        launch_kwargs = {
            "headless": True,
            "timeout": self.options.timeout_ms,
        }

        browser_path = self._browser_path()
        if browser_path:
            return await p.chromium.launch(executable_path=browser_path, **launch_kwargs)

        with suppress(Exception):
            return await p.chromium.launch(channel="msedge", **launch_kwargs)
        with suppress(Exception):
            return await p.chromium.launch(channel="chrome", **launch_kwargs)
        try:
            return await p.chromium.launch(**launch_kwargs)
        except Exception as exc:
            raise FeedRenderError(
                "无法启动 Playwright 浏览器，请安装 Chromium 或在配置里填写 playwright_browser_path"
            ) from exc

    def _browser_path(self) -> str:
        configured = self.options.browser_path.strip()
        if configured and Path(configured).exists():
            return configured

        for path in _candidate_browser_paths():
            if Path(path).exists():
                return path
        return ""

    def _homepage_region_locator(self, page: Any, source: FeedSource) -> Any:
        if source.key == "contests":
            return page.locator("div.summercontest")
        return page.locator("div.content-panel").filter(has_text=source.homepage_heading or source.title)

    async def _region_matches_items(self, locator: Any, items: list[FeedItem]) -> bool:
        with suppress(Exception):
            text = await locator.inner_text(timeout=3000)
            markers = []
            for item in items:
                markers.extend((item.title, item.fullname))
            return any(marker and marker in text for marker in markers)
        return False

    async def _settle_page(self, page: Any) -> None:
        with suppress(Exception):
            await page.wait_for_timeout(1600)

    async def _wait_for_images(self, page: Any) -> None:
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=min(10000, self.options.timeout_ms))
        with suppress(Exception):
            await page.wait_for_function(
                """() => Array.from(document.images).every((img) => {
                    if (!img.offsetParent && getComputedStyle(img).display === "none") return true;
                    return img.complete && img.naturalWidth > 0;
                })""",
                timeout=min(10000, self.options.timeout_ms),
            )
        with suppress(Exception):
            await page.wait_for_timeout(1200)

    async def _hide_noisy_page_parts(self, page: Any) -> None:
        with suppress(Exception):
            await page.add_style_tag(
                content="""
                #navi-bar,
                #header,
                #side-bar,
                #page-options-container,
                #page-options-bottom,
                .page-rate-widget-box,
                .page-watch-options,
                .licensebox {
                    display: none !important;
                }
                """
            )

    def _build_daily_html(
        self,
        sections: dict[str, list[FeedItem]],
        errors: dict[str, str],
        source_order: tuple[str, ...],
        sources: dict[str, FeedSource],
    ) -> str:
        section_html = []
        for source_key in source_order:
            source = sources[source_key]
            items = sections.get(source_key, [])
            section_title = self._daily_section_title(source, items)
            cards = "".join(self._daily_card(item, index) for index, item in enumerate(items, start=1))
            if not cards:
                message = errors.get(source_key) or "暂无可用内容。"
                cards = f"<article class='card empty'>{html.escape(message)}</article>"
            section_html.append(
                "<section class='section'>"
                f"<div class='section-title'><span class='mark'></span>{html.escape(section_title)}</div>"
                f"{cards}"
                "</section>"
            )

        today = datetime.now().strftime("%Y-%m-%d")
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>{DAILY_REPORT_CSS}</style>
</head>
<body>
  <main class="report">
    <header class="header">
      <div class="brand">
        <img class="foundation-logo" src="{html.escape(SCP_FOUNDATION_LOGO_URL)}" alt="SCP 基金会 Logo">
        <div>
          <div class="kicker">SCP-CN Feed</div>
          <h1>中文站日报</h1>
        </div>
      </div>
      <div class="date">{today}</div>
    </header>
    {''.join(section_html)}
    <footer class="footer">数据来自 SCP 中文站首页模块，失败时回退 RSS / 标签页。日报卡片由本地 Playwright 渲染。</footer>
  </main>
</body>
</html>"""

    def _daily_section_title(self, source: FeedSource, items: list[FeedItem]) -> str:
        if source.key == "contests":
            return "竞赛新闻"
        return source.title

    def _daily_card(self, item: FeedItem, index: int) -> str:
        meta: list[tuple[str, str]] = []
        if item.created_by:
            meta.append(("作者", item.created_by))
        if item.rating is not None:
            meta.append(("评分", str(item.rating)))
        if not meta:
            meta.append(("来源", "首页模块"))

        summary = self._daily_summary_html(item)
        title = html.escape(item.title)
        url = html.escape(item.url)
        link_label = "竞赛链接" if item.source_key == "contests" else "文章链接"
        meta_html = "".join(
            f'<span><span class="label">{html.escape(label)}</span>：{html.escape(value)}</span>'
            for label, value in meta
        )
        return f"""
<article class="card">
  <div class="index">{index:02d}</div>
  <div class="card-body">
    <h2>{title}</h2>
    <div class="meta">{meta_html}</div>
    <p>{summary}</p>
    <div class="url"><span class="label">{link_label}</span>：{url}</div>
  </div>
</article>"""

    def _daily_summary_html(self, item: FeedItem) -> str:
        limit = 230 if item.source_key != "contests" else 420
        compact = _compact_text(item.summary or "暂无摘要", limit)
        if item.summary_html and item.summary and len(" ".join(item.summary.split())) <= limit:
            return item.summary_html
        return html.escape(compact)

    def _new_output_path(self, slug: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", slug).strip("_") or "render"
        return self.output_dir / f"scp_cn_feed_{timestamp}_{safe_slug}.png"


def _compact_text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _candidate_browser_paths() -> tuple[str, ...]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    return tuple(
        path
        for path in (
            str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        )
        if path and not path.startswith(".")
    )


DAILY_REPORT_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #eef1f4;
  color: #171b1f;
  font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", "PingFang SC", Arial, sans-serif;
}
.report {
  width: 980px;
  min-height: 1280px;
  padding: 44px 48px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.92), rgba(236,240,241,.96)),
    radial-gradient(circle at 18% 14%, rgba(175, 43, 48, .12), transparent 28%);
}
.header {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 24px;
  padding-bottom: 22px;
  border-bottom: 3px solid #20262c;
}
.brand {
  display: flex;
  align-items: center;
  gap: 20px;
}
.foundation-logo {
  display: block;
  width: 88px;
  height: 88px;
  object-fit: contain;
}
.kicker {
  color: #2367a5;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 18px;
}
h1 {
  margin: 8px 0 0;
  font-size: 46px;
  line-height: 1.05;
  letter-spacing: 0;
}
.date {
  color: #596864;
  font-size: 18px;
}
.section {
  margin-top: 32px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 25px;
  font-weight: 800;
}
.mark {
  width: 14px;
  height: 34px;
  border-radius: 2px;
  background: #b3272d;
}
.card {
  position: relative;
  display: grid;
  grid-template-columns: 58px 1fr;
  gap: 18px;
  margin-top: 16px;
  padding: 22px 24px;
  border: 1px solid #d9dee0;
  border-left: 6px solid #334f63;
  border-radius: 8px;
  background: rgba(255,255,255,.88);
  box-shadow: 0 10px 26px rgba(22,31,42,.08);
}
.card.empty {
  display: block;
  color: #596864;
  font-size: 18px;
}
.index {
  align-self: start;
  width: 52px;
  height: 52px;
  border: 1px solid #d0d6d8;
  border-radius: 50%;
  color: #b3272d;
  display: grid;
  place-items: center;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 20px;
  font-weight: 800;
}
h2 {
  margin: 0 0 10px;
  font-size: 25px;
  line-height: 1.28;
  letter-spacing: 0;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
  color: #2367a5;
  font-size: 15px;
}
.meta .label {
  color: inherit;
}
p {
  margin: 0;
  color: #283235;
  font-size: 18px;
  line-height: 1.72;
}
.url {
  margin-top: 14px;
  color: #506579;
  font-size: 14px;
  word-break: break-all;
}
.label {
  color: #b3272d;
  font-weight: 800;
}
.summary-link {
  color: #b3272d;
  font-weight: 800;
}
.footer {
  margin-top: 34px;
  padding-top: 18px;
  border-top: 1px solid #cbd2d5;
  color: #6d7775;
  font-size: 14px;
}
"""
