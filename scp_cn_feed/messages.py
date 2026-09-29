"""把条目排成纯文字、QQ Markdown 和按钮。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .models import SECTIONS, SITE_BASE_URL, FeedItem


TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
BLOCK_RE = re.compile(r"<!--\s*区块开始\s*-->(.*?)<!--\s*区块结束\s*-->", re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
FIELD_RE = re.compile(r"\{(\w+)\}")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
MD_SUMMARY_LIMIT = 90
TEXT_SUMMARY_LIMIT = 140
COMMAND_BUTTONS = (
    ("cmd_report", "🗞️ 日报", "/scp 日报", 4),
    ("cmd_sub", "🔔 订阅", "/scp 订阅", 1),
    ("cmd_unsub", "🔕 退订", "/scp 退订", 3),
    ("cmd_status", "📡 状态", "/scp 状态", 0),
)
HELP_TEXT = (
    "SCP-CN 推送\n"
    "/scp 日报 — 看首页当前的精品和竞赛（直接发 /scp 也行）\n"
    "/scp 订阅 — 本会话接收新内容推送\n"
    "/scp 退订 — 取消推送\n"
    "/scp 状态 — 查看订阅状态\n"
    "推送哪些区块、用什么形式，去插件配置里改。"
)


def headline(new: set[str] | None) -> str:
    """new 为 None 是日报，否则是推送，里面装着换了内容的区块。"""
    return "日报" if new is None else "新内容通报"


def format_text(items: list[FeedItem], *, new: set[str] | None = None, today: date | None = None) -> str:
    today = today or date.today()
    blocks = [f"【SCP-CN {headline(new)}】{today:%Y-%m-%d}"]
    for item in items:
        section = SECTIONS[item.section]
        lines = [f"{section.icon} {section.title}{_tag(item)}{_new_mark(item, new)}", item.title]
        if item.author:
            lines.append(f"作者：{item.author}")
        if item.summary:
            lines.append(clip(item.summary, TEXT_SUMMARY_LIMIT))
        lines.append(item.url)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_markdown(
    items: list[FeedItem],
    *,
    new: set[str] | None = None,
    links: bool = True,
    today: date | None = None,
) -> str:
    """按 templates 里的日报 / 推送模板填空，每次现读文件，改模板不用重载插件。"""
    template = (TEMPLATE_DIR / ("report.md" if new is None else "push.md")).read_text(encoding="utf-8")
    match = BLOCK_RE.search(template)
    if not match:
        raise ValueError("Markdown 模板里缺少 <!-- 区块开始 --> / <!-- 区块结束 --> 标记")

    page = {"date": f"{(today or date.today()):%Y-%m-%d}"}
    blocks = []
    for item in items:
        section = SECTIONS[item.section]
        blocks.append(
            _fill(
                match.group(1),
                {
                    "icon": section.icon,
                    "section": f"{section.title}{_tag(item)}",
                    "title": item.title,
                    "url": item.url,
                    "link": f"[{item.title}]({item.url})" if links else item.title,
                    "author": item.author or "",
                    "summary": clip_markdown(item.summary_md or item.summary or "", MD_SUMMARY_LIMIT, links=links),
                    "new": "🆕" if new and item.section in new else "",
                },
            ).strip("\n")
        )
    text = _fill(template[: match.start()], page) + "\n\n".join(blocks) + _fill(template[match.end() :], page)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fill(text: str, fields: dict[str, str]) -> str:
    """去掉注释再填占位符；一行里的占位符全是空值时整行删掉。"""
    lines = []
    for line in COMMENT_RE.sub("", text).split("\n"):
        known = [name for name in FIELD_RE.findall(line) if name in fields]
        if known and not any(fields[name] for name in known):
            continue
        lines.append(FIELD_RE.sub(lambda m: fields.get(m.group(1), m.group(0)), line).rstrip())
    return "\n".join(lines)


def format_markdown_text(text: str) -> str:
    """把指令回复这类纯文字包成 Markdown，首行当标题。"""
    head, _, body = text.partition("\n")
    return f"### 🗂️ {head}\n{body}" if body else f"### 🗂️ {head}"


def build_keyboard(*, links: bool = True) -> dict:
    """第一行是跳中文站首页的按钮，第二行是指令按钮；原文链接放在 Markdown 标题上。"""
    rows = []
    if links:
        rows.append({"buttons": [_button("link_home", "SCP基金会", SITE_BASE_URL + "/", action=0, style=1)]})
    rows.append(
        {
            "buttons": [
                _button(button_id, label, data, action=2, style=style)
                for button_id, label, data, style in COMMAND_BUTTONS
            ]
        }
    )
    return {"content": {"rows": rows}}


def clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit].rstrip() + "…"


def clip_markdown(text: str, limit: int, *, links: bool = True) -> str:
    """按看得见的字数截断，不会把链接截坏；links=False 时链接改成粗体。"""
    compact = " ".join(text.split())
    tokens: list[tuple[str, str | None]] = []
    pos = 0
    for match in LINK_RE.finditer(compact):
        tokens += [(compact[pos : match.start()], None), (match.group(1), match.group(2))]
        pos = match.end()
    tokens.append((compact[pos:], None))

    parts, used = [], 0
    for label, url in tokens:
        cut = label[: limit - used]
        used += len(cut)
        if cut:
            parts.append(cut if url is None else f"[{cut}]({url})" if links else f"**{cut}**")
        if len(cut) < len(label):
            return "".join(parts).rstrip() + "…"
    return "".join(parts)


def _new_mark(item: FeedItem, new: set[str] | None) -> str:
    return " 🆕" if new and item.section in new else ""


def _tag(item: FeedItem) -> str:
    return f" · {item.tag}" if item.tag else ""


def _button(button_id: str, label: str, data: str, *, action: int, style: int) -> dict:
    button_action = {
        "type": action,
        "permission": {"type": 2},
        "data": data,
        "unsupport_tips": "当前 QQ 版本不支持按钮，请升级",
    }
    if action == 2:
        button_action["enter"] = True
    return {
        "id": button_id,
        "render_data": {"label": label, "visited_label": label, "style": style},
        "action": button_action,
    }
