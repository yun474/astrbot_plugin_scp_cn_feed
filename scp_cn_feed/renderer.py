from __future__ import annotations

import hashlib
import html
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from .messages import clip, headline
from .models import SECTIONS, FeedItem


CARD_WIDTH = 760
TIMEOUT_MS = 30000
RETENTION_SECONDS = 2 * 86400
SUMMARY_LIMIT = 180

# SCP 基金会徽标（CC BY-SA 3.0），线条用 currentColor，方便换色。
_ARROW = "m64.7 30.6v24h-5.08l8.08 14 8.08-14h-5.08l-.000265-24h-5.99"
SCP_LOGO_SVG = (
    '<svg viewBox="0 0 135 135" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="67.7" cy="71.5" r="33" fill="none" stroke="currentColor" stroke-width="6"/>'
    '<path d="m51.9 11.9h31.7l3.07 11.4.944.391c19.4 8.03 32 26.9 32 47.9 0 2.26-.149 4.53-.445 '
    "6.77l-.133 1.01 8.37 8.37-15.8 27.4-11.4-3.06-.809.623c-9.06 6.95-20.2 10.7-31.6 10.7-11.4 "
    "6e-5-22.5-3.77-31.6-10.7l-.81-.623-11.4 3.06-15.8-27.4 8.37-8.37-.133-1.01c-.296-2.25-.445-4.51"
    '-.445-6.77.000141-21 12.6-39.9 32-47.9l.944-.391z" fill="none" stroke="currentColor" stroke-width="4"/>'
    f'<g fill="currentColor"><path d="{_ARROW}"/>'
    f'<path d="{_ARROW}" transform="rotate(120 67.7 71.5)"/>'
    f'<path d="{_ARROW}" transform="rotate(240 67.7 71.5)"/></g>'
    "</svg>"
)


class RenderError(RuntimeError):
    pass


class CardRenderer:
    def __init__(self, output_dir: Path, browser_path: str = ""):
        self.output_dir = output_dir
        self.browser_path = browser_path.strip()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def render(self, items: list[FeedItem], *, new: set[str] | None = None) -> Path:
        page = build_card_html(items, new=new)
        # 同一天同样的内容只渲染一次，多个会话推送时直接复用。
        path = self.output_dir / f"scp_cn_{hashlib.sha1(page.encode()).hexdigest()[:16]}.png"
        if path.exists():
            return path
        self._prune()

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RenderError("没装 playwright，没法渲染图片") from exc

        temp_path = path.with_suffix(".tmp.png")
        async with async_playwright() as p:
            browser = await self._launch(p)
            try:
                tab = await browser.new_page(
                    viewport={"width": CARD_WIDTH, "height": 800},
                    device_scale_factor=2,
                )
                await tab.set_content(page, wait_until="load", timeout=TIMEOUT_MS)
                await tab.locator(".dossier").screenshot(path=str(temp_path), timeout=TIMEOUT_MS)
            finally:
                await browser.close()
        os.replace(temp_path, path)
        return path

    async def _launch(self, p: Any) -> Any:
        attempts = [{"executable_path": self.browser_path}] if self.browser_path else []
        attempts += [{}, {"channel": "msedge"}, {"channel": "chrome"}]
        error: Exception | None = None
        for options in attempts:
            try:
                return await p.chromium.launch(headless=True, timeout=TIMEOUT_MS, **options)
            except Exception as exc:
                error = exc
        raise RenderError(
            "启动浏览器失败：请执行 playwright install chromium，或在配置里填写浏览器路径"
        ) from error

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        for old in self.output_dir.glob("scp_cn_*.png"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)


def build_card_html(items: list[FeedItem], *, new: set[str] | None = None, today: date | None = None) -> str:
    update = new is not None
    today = today or date.today()
    entries = "".join(
        _entry(item, index, fresh=update and item.section in new) for index, item in enumerate(items, start=1)
    )
    title_en = "NEW CONTENT ALERT" if update else "DAILY BRIEFING"
    doc_type = "ALERT" if update else "BRIEF"
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><style>{CARD_CSS}</style></head>
<body>
<main class="dossier">
  <div class="watermark">{SCP_LOGO_SVG}</div>
  <header class="masthead">
    <div class="logo">{SCP_LOGO_SVG}</div>
    <div class="org">
      <div class="org-en">SCP FOUNDATION · CN BRANCH</div>
      <div class="org-cn">SCP 基金会中文分部</div>
      <div class="motto">SECURE · CONTAIN · PROTECT</div>
    </div>
    <div class="clearance"><b>LEVEL 1</b><span>公开 / UNRESTRICTED</span></div>
  </header>
  <div class="hazard"></div>
  <section class="docline">
    <div><span>文档编号</span><b>SCP-CN/{doc_type}/{today:%Y%m%d}</b></div>
    <div><span>签发日期</span><b>{today:%Y-%m-%d}</b></div>
    <div><span>收录条目</span><b>{len(items):02d}</b></div>
  </section>
  <h1 class="title{' alert' if update else ''}">{headline(new)}<small>{title_en}</small></h1>
  <div class="entries">{entries}</div>
  <footer class="footer">
    <div>安保 · 收容 · 保护</div>
    <div class="source">数据来源 scp-wiki-cn.wikidot.com ｜ 授权人员 <i></i> 已阅</div>
  </footer>
</main>
</body>
</html>"""


def _entry(item: FeedItem, index: int, *, fresh: bool) -> str:
    section = SECTIONS[item.section]
    esc = html.escape
    parts = [
        f'<article class="entry{" fresh" if fresh else ""}">',
        '<div class="entry-head">',
        f'<span class="no">{index:02d}</span>',
        f'<span class="sec">{esc(section.title)}</span>',
        f'<span class="en">{esc(section.en)}</span>',
    ]
    if item.tag:
        parts.append(f'<span class="tag">{esc(item.tag)}</span>')
    parts.append("</div>")
    if fresh:
        parts.append('<div class="stamp">NEW<small>新收录</small></div>')
    if item.image_url:
        parts.append(f'<img class="banner" src="{esc(item.image_url)}" onerror="this.remove()">')
    parts.append(f"<h2>{esc(item.title)}</h2>")
    if item.author:
        parts.append(f'<div class="author"><span>作者</span>{esc(item.author)}</div>')
    if item.summary:
        parts.append(f'<blockquote class="{item.section}">{_summary_html(item)}</blockquote>')
    link = item.url.removeprefix("https://")
    parts.append(f'<div class="url"><span>▸ 档案位置</span>{esc(link)}</div>')
    parts.append("</article>")
    return "".join(parts)


def _summary_html(item: FeedItem) -> str:
    compact = " ".join(item.summary.split())
    if item.summary_html and len(compact) <= SUMMARY_LIMIT:
        return item.summary_html
    return html.escape(clip(compact, SUMMARY_LIMIT))


CARD_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --ink: #16161a;
  --paper: #f3efe6;
  --card: #fffdf8;
  --line: #d9d1bf;
  --muted: #7b7263;
  --red: #a3151b;
  --sans: "Noto Sans SC", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "WenQuanYi Micro Hei", sans-serif;
  --serif: "Noto Serif SC", "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", "SimSun", serif;
  --mono: "JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Menlo", monospace;
}
body { background: var(--paper); color: var(--ink); font-family: var(--sans); }
.dossier {
  position: relative;
  width: 760px;
  overflow: hidden;
  background:
    radial-gradient(circle at 20% 0%, rgba(255,255,255,.7), transparent 45%),
    repeating-linear-gradient(0deg, transparent 0 31px, rgba(22,22,26,.035) 31px 32px),
    var(--paper);
}
.watermark {
  position: absolute;
  right: -90px;
  top: 250px;
  width: 470px;
  color: var(--ink);
  opacity: .045;
  pointer-events: none;
}
.masthead {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 26px 32px 24px;
  background: linear-gradient(180deg, #1d1d22, #0e0e11);
  color: #f5f2ea;
}
.logo { width: 76px; height: 76px; color: #f5f2ea; flex: none; }
.org { flex: 1; }
.org-en { font: 700 12px/1 var(--mono); letter-spacing: .28em; color: #a9a397; }
.org-cn { margin-top: 8px; font-size: 30px; font-weight: 900; letter-spacing: .06em; }
.motto { margin-top: 8px; font: 600 11px/1 var(--mono); letter-spacing: .42em; color: #e0474d; }
.clearance {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  padding: 10px 14px;
  border: 1px solid #4a4640;
  border-left: 4px solid var(--red);
}
.clearance b { font: 800 20px/1 var(--mono); letter-spacing: .12em; }
.clearance span { font-size: 11px; color: #a9a397; letter-spacing: .1em; }
.hazard {
  height: 10px;
  background: repeating-linear-gradient(-45deg, var(--red) 0 14px, #0e0e11 14px 28px);
}
.docline {
  display: flex;
  justify-content: space-between;
  margin: 20px 32px 0;
  padding-bottom: 12px;
  border-bottom: 1px dashed #b9b09c;
  font-family: var(--mono);
}
.docline div { display: flex; flex-direction: column; gap: 5px; }
.docline span { font-size: 11px; color: var(--muted); letter-spacing: .12em; }
.docline b { font-size: 15px; letter-spacing: .04em; }
.title {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin: 22px 32px 4px;
  font-size: 38px;
  font-weight: 900;
  letter-spacing: .08em;
}
.title::before {
  content: "";
  align-self: stretch;
  width: 8px;
  background: var(--ink);
}
.title.alert::before { background: var(--red); }
.title small { font: 700 13px/1 var(--mono); letter-spacing: .3em; color: var(--red); }
.entries { position: relative; padding: 14px 32px 8px; }
.entry {
  position: relative;
  margin-bottom: 18px;
  padding: 0 24px 18px;
  background: var(--card);
  border: 1px solid var(--line);
  border-left: 5px solid var(--ink);
  box-shadow: 0 1px 0 #fff inset, 0 10px 24px -18px rgba(22,22,26,.55);
}
.entry.fresh { border-left-color: var(--red); }
.entry-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 -24px 16px;
  padding: 9px 24px;
  background: #ece6d8;
  border-bottom: 1px solid var(--line);
}
.entry-head .no {
  padding: 3px 7px;
  background: var(--ink);
  color: #f5f2ea;
  font: 800 13px/1 var(--mono);
}
.entry.fresh .entry-head .no { background: var(--red); }
.entry-head .sec { font-size: 16px; font-weight: 800; letter-spacing: .06em; }
.entry-head .en { font: 600 11px/1 var(--mono); letter-spacing: .2em; color: var(--muted); }
.entry-head .tag {
  margin-left: auto;
  padding: 3px 9px;
  border: 1px solid var(--ink);
  font-size: 12px;
  font-weight: 700;
}
.stamp {
  position: absolute;
  top: 46px;
  right: 22px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 5px 12px 4px;
  border: 3px double var(--red);
  border-radius: 4px;
  color: var(--red);
  font: 900 22px/1 var(--mono);
  letter-spacing: .2em;
  transform: rotate(-9deg);
  opacity: .82;
}
.stamp small { margin-top: 3px; font: 800 10px/1 var(--sans); letter-spacing: .3em; }
.banner { display: block; width: 100%; margin-bottom: 14px; border: 1px solid var(--line); }
h2 { font-size: 25px; line-height: 1.35; font-weight: 900; letter-spacing: .02em; }
.fresh h2 { padding-right: 96px; }
.author { margin-top: 8px; font-size: 14px; color: #3f3a33; }
.author span {
  margin-right: 8px;
  padding: 1px 6px;
  background: var(--ink);
  color: #f5f2ea;
  font-size: 11px;
  letter-spacing: .15em;
}
blockquote {
  margin-top: 14px;
  padding: 12px 18px;
  background: #f7f3ea;
  border-left: 3px solid #c9bea7;
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.8;
  color: #2d2924;
}
blockquote.contests { font-family: var(--sans); font-size: 15px; }
.ref { color: var(--red); font-weight: 800; }
.url {
  margin-top: 14px;
  font: 12px/1.4 var(--mono);
  color: var(--muted);
  word-break: break-all;
}
.url span { margin-right: 10px; color: var(--ink); font-weight: 700; font-family: var(--sans); }
.footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 14px;
  padding: 16px 32px;
  background: #0e0e11;
  color: #d8d2c4;
  font-size: 13px;
  letter-spacing: .3em;
}
.footer .source { font: 11px/1 var(--mono); letter-spacing: .04em; color: #8f887b; }
.footer i { display: inline-block; width: 46px; height: 11px; margin: 0 4px; background: #d8d2c4; vertical-align: -1px; }
"""
