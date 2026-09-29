import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Filter:
    @staticmethod
    def command(_name):
        return lambda function: function


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    @classmethod
    def fromFileSystem(cls, path):
        image = cls()
        image.path = path
        return image


class _MessageChain:
    def __init__(self, components=None):
        self.components = list(components or [])

    def message(self, text):
        self.components.append(_Plain(text))
        return self


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules.setdefault(name, module)


_module("astrbot")
_module("astrbot.api", AstrBotConfig=dict, logger=_Logger())
_module("astrbot.api.event", AstrMessageEvent=object, MessageChain=_MessageChain, filter=_Filter())
_module("astrbot.api.message_components", Image=_Image)
_module("astrbot.api.star", Context=object, Star=object, StarTools=object)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_scp_cn_feed.main import ScpCnFeedPlugin  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.fetcher import parse_homepage  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.messages import (  # noqa: E402
    build_keyboard,
    clip_markdown,
    format_markdown,
    format_text,
)
from astrbot_plugin_scp_cn_feed.scp_cn_feed.models import SECTIONS, FeedItem  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.qq_official import QQOfficialTarget  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.renderer import build_card_html  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.store import AnchorStore  # noqa: E402


HOMEPAGE = """
<div id="page-content">
  <div class="content-panel centered standalone"><p>站务公告，别点陌生链接。</p></div>
  <div class="content-panel centered standalone">
    <p>掌声献给<a href="/summer-contest-2026">幕前</a>与<a href="/summer-contest-2026">2026夏季征文</a>！</p>
    <p>感谢每一位作者。</p>
  </div>
  <div class="summercontest"><a href="https://scp-wiki-cn.wikidot.com/summer-contest-2026">
    <img src="https://example.invalid/banner.jpg"></a></div>
  <div class="content-panel left-column">
    <div class="panel-heading"><p>精品原创SCP</p></div>
    <div class="panel-body">
      <div class="feature-title"><p><a href="/scp-cn-4003">SCP-CN-4003：命名混沌</a></p></div>
      <div class="feature-subtitle"><p>by <span class="printuser"><a href="#">H-Storm Z</a></span></p></div>
      <p><em>“置身妖野，我沐浴<a href="/x">丁香</a>。”</em></p>
    </div>
  </div>
  <div class="content-panel right-column">
    <div class="panel-heading"><p>主题精品：观谬维基</p></div>
    <div class="panel-body">
      <div class="feature-title"><p><a href="/parawatch">所以我放弃了观谬维基</a></p></div>
      <div class="feature-subtitle"><p>by Rye Travis</p></div>
    </div>
  </div>
  <div class="news-block content-panel"><div class="panel-body">
    <div class="news-title"><p>2026年7月26日</p></div>
    <div class="news-content"><p>
      <span class="printuser"><a href="https://www.wikidot.com/user:info/a">某人</a></span>解开了<a href="/puzzle">谜题</a>！家具城闭店整改。
    </p></div>
  </div></div>
</div>
"""


def _item(section, uid, title="标题"):
    return FeedItem(section=section, uid=uid, title=title, url=f"https://scp-wiki-cn.wikidot.com/{uid}")


class _Config(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


class _Platform:
    def __init__(self, name="aiocqhttp", platform_id="bot"):
        self._meta = types.SimpleNamespace(name=name, id=platform_id)
        self.api = _QQApi()

    def meta(self):
        return self._meta

    def get_client(self):
        return types.SimpleNamespace(api=self.api)


class _QQApi:
    def __init__(self):
        self.calls = []

    async def post_group_message(self, **payload):
        self.calls.append(("group", payload))

    async def post_c2c_message(self, **payload):
        self.calls.append(("c2c", payload))


class _Context:
    def __init__(self, *platforms, ok=True):
        self.platform_manager = types.SimpleNamespace(platform_insts=list(platforms))
        self.sent = []
        self.ok = ok

    async def send_message(self, origin, chain):
        self.sent.append((origin, chain))
        return self.ok


class _Fetcher:
    def __init__(self, report):
        self.report = report

    async def fetch(self, fresh=False):
        return self.report


def _plugin(temp_dir, report, context=None, **config):
    plugin = object.__new__(ScpCnFeedPlugin)
    plugin.config = _Config(report_mode="text", push_mode="text", **config)
    plugin.context = context or _Context(_Platform())
    plugin.store = AnchorStore(Path(temp_dir) / "state.json")
    plugin.fetcher = _Fetcher(report)
    return plugin


class ParseTests(unittest.TestCase):
    def test_homepage_blocks(self):
        report = parse_homepage(HOMEPAGE)

        scp = report["featured_scp"]
        self.assertEqual(scp.item_id, "featured_scp:scp-cn-4003")
        self.assertEqual(scp.author, "H-Storm Z")
        self.assertEqual(scp.summary, "“置身妖野，我沐浴丁香。”")
        self.assertIn('<b class="ref">丁香</b>', scp.summary_html)

        theme = report["featured_theme"]
        self.assertEqual((theme.tag, theme.author), ("观谬维基", "Rye Travis"))

        contest = report["contests"]
        self.assertEqual(contest.title, "2026夏季征文")
        self.assertEqual(contest.image_url, "https://example.invalid/banner.jpg")
        self.assertEqual(contest.summary, "掌声献给幕前与2026夏季征文！\n感谢每一位作者。")
        self.assertEqual(
            contest.summary_md,
            "掌声献给[幕前](https://scp-wiki-cn.wikidot.com/summer-contest-2026)"
            "与[2026夏季征文](https://scp-wiki-cn.wikidot.com/summer-contest-2026)！ 感谢每一位作者。",
        )

        self.assertNotIn("news", report)


class MessageTests(unittest.TestCase):
    def test_keyboard(self):
        rows = build_keyboard()["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [1, 4])
        home = rows[0]["buttons"][0]
        self.assertEqual(home["render_data"]["label"], "SCP基金会")
        self.assertEqual((home["action"]["type"], home["action"]["data"]), (0, "https://scp-wiki-cn.wikidot.com/"))
        command = rows[-1]["buttons"][1]
        self.assertEqual((command["action"]["type"], command["action"]["data"]), (2, "/scp 订阅"))
        labels = [button["render_data"]["label"] for row in rows for button in row["buttons"]]
        self.assertTrue(all(len(label) <= 10 for label in labels))

    def test_markdown_layout(self):
        items = [
            FeedItem("featured_scp", "a", "SCP-CN-1", "https://x.invalid/a", author="作者甲", summary="“引言”"),
            FeedItem(
                "contests",
                "c",
                "征文",
                "https://x.invalid/c",
                summary="献给征文！",
                summary_md="献给[征文](https://x.invalid/c)！",
            ),
        ]
        pushed = format_markdown(items, new={"featured_scp"}, today=date(2026, 9, 30))
        self.assertEqual(
            pushed,
            "# 🚨 SCP-CN 新内容通报\n> 2026-09-30 ｜ 安保 · 收容 · 保护\n\n***\n\n"
            "## ☣️ 精品原创 SCP\n**[SCP-CN-1](https://x.invalid/a)**\n> *作者* *作者甲*\n\n*“引言”*\n\n"
            "## 🏆 竞赛与活动\n**[征文](https://x.invalid/c)**\n\n*献给[征文](https://x.invalid/c)！*\n\n"
            "***\n📡 数据来源：SCP 基金会中文分部首页",
        )
        self.assertTrue(format_markdown(items).startswith("# 🗂️ SCP-CN 日报\n"))
        plain = format_markdown(items, links=False)
        self.assertNotIn("](", plain)
        self.assertIn("*献给**征文**！*", plain)

    def test_clip_markdown_keeps_links_whole(self):
        text = "开头[链接文字](https://x.invalid/a)结尾"
        self.assertEqual(clip_markdown(text, 20), text)
        self.assertEqual(clip_markdown(text, 4), "开头[链接](https://x.invalid/a)…")
        self.assertEqual(clip_markdown(text, 20, links=False), "开头**链接文字**结尾")
        self.assertEqual(clip_markdown(text, 2), "开头…")

    def test_text_and_card(self):
        items = [_item("featured_scp", "scp-cn-1", "<危险>")]
        self.assertIn("https://scp-wiki-cn.wikidot.com/scp-cn-1", format_text(items))
        card = build_card_html(items, new={"featured_scp"})
        self.assertIn("&lt;危险&gt;", card)
        self.assertIn('class="stamp"', card)
        self.assertIn("<svg", card)
        self.assertNotIn('class="stamp"', build_card_html(items, new=set()))
        self.assertIn("DAILY BRIEFING", build_card_html(items))


class QQOfficialTests(unittest.TestCase):
    def test_resolve_targets(self):
        qq = _Platform("qq_official")
        group = QQOfficialTarget.resolve(qq, "qq:GroupMessage:user_ABCDEF0123")
        self.assertEqual((group.scene, group.openid), ("group", "ABCDEF0123"))
        self.assertEqual(QQOfficialTarget.resolve(qq, "qq:FriendMessage:ABC").scene, "c2c")
        self.assertIsNone(QQOfficialTarget.resolve(qq, "qq:GroupMessage:123456"))
        self.assertIsNone(QQOfficialTarget.resolve(_Platform(), "bot:GroupMessage:ABC"))

    def test_passive_markdown_payload(self):
        qq = _Platform("qq_official")
        target = QQOfficialTarget.resolve(qq, "qq:GroupMessage:ABC", msg_id="m1")
        asyncio.run(target.send_markdown("# hi", {"content": {"rows": []}}))
        scene, payload = qq.api.calls[0]
        self.assertEqual(scene, "group")
        self.assertEqual(payload["group_openid"], "ABC")
        self.assertEqual((payload["msg_type"], payload["msg_id"]), (2, "m1"))
        self.assertEqual(payload["markdown"], {"content": "# hi"})


class PushTests(unittest.TestCase):
    def test_push_shows_selected_sections_and_marks_changed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            origin = "bot:GroupMessage:1"
            report = {
                "featured_scp": _item("featured_scp", "a"),
                "featured_tale": _item("featured_tale", "t1", "旧故事"),
                "contests": _item("contests", "c1"),
            }
            plugin = _plugin(
                temp_dir,
                report,
                subscribed_sessions=[origin],
                push_sections=["featured_scp", "featured_tale"],
            )

            asyncio.run(plugin.check_updates())
            self.assertEqual(plugin.context.sent, [])
            self.assertEqual(plugin.store.anchors(origin)["contests"], "contests:c1")

            # 没勾选的区块换了内容不推送，但会记下来。
            report["contests"] = _item("contests", "c2")
            asyncio.run(plugin.check_updates())
            self.assertEqual(plugin.context.sent, [])
            self.assertEqual(plugin.store.anchors(origin)["contests"], "contests:c2")

            report["featured_scp"] = _item("featured_scp", "b", "新精品")
            asyncio.run(plugin.check_updates())
            self.assertEqual(len(plugin.context.sent), 1)
            text = plugin.context.sent[0][1].components[0].text
            self.assertIn("☣️ 精品原创 SCP 🆕\n新精品", text)
            self.assertIn("📖 精品原创故事\n旧故事", text)
            self.assertNotIn("竞赛与活动", text)
            self.assertEqual(plugin.store.anchors(origin)["featured_scp"], "featured_scp:b")

    def test_failed_send_keeps_anchor_for_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            origin = "bot:GroupMessage:1"
            plugin = _plugin(
                temp_dir,
                {"featured_scp": _item("featured_scp", "b")},
                context=_Context(_Platform(), ok=False),
                subscribed_sessions=[origin],
            )
            plugin.store.update(origin, {"featured_scp": "featured_scp:a"})
            asyncio.run(plugin.check_updates())
            self.assertEqual(plugin.store.anchors(origin)["featured_scp"], "featured_scp:a")

    def test_qq_official_markdown_push_bypasses_astrbot_session_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            origin = "qq:GroupMessage:ABC"
            qq = _Platform("qq_official", "qq")
            plugin = _plugin(
                temp_dir,
                {"featured_scp": _item("featured_scp", "b")},
                context=_Context(qq),
                subscribed_sessions=[origin],
            )
            plugin.config["push_mode"] = "markdown"
            plugin.store.update(origin, {"featured_scp": "featured_scp:a"})
            asyncio.run(plugin.check_updates())

            self.assertEqual(plugin.context.sent, [])
            _scene, payload = qq.api.calls[0]
            self.assertNotIn("msg_id", payload)
            self.assertIn("keyboard", payload)
            self.assertEqual(plugin.store.anchors(origin)["featured_scp"], "featured_scp:b")


class ReportTests(unittest.TestCase):
    def test_report_uses_its_own_settings(self):
        class Event:
            unified_msg_origin = "bot:GroupMessage:1"

            def plain_result(self, text):
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            report = {"featured_scp": _item("featured_scp", "a", "精品"), "contests": _item("contests", "c", "征文")}
            plugin = _plugin(temp_dir, report, report_sections=["contests"], push_sections=["featured_scp"])

            async def collect():
                return [result async for result in plugin._reply_report(Event())]

            (text,) = asyncio.run(collect())
            self.assertIn("SCP-CN 日报", text)
            self.assertIn("征文", text)
            self.assertNotIn("精品", text)


class ConfigTests(unittest.TestCase):
    def test_subscribe_and_unsubscribe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = _plugin(temp_dir, {"contests": _item("contests", "c1")})
            self.assertIn("订阅成功", asyncio.run(plugin._subscribe("s:1")))
            self.assertEqual(plugin.config["subscribed_sessions"], ["s:1"])
            self.assertEqual(plugin.store.anchors("s:1"), {"contests": "contests:c1"})

            self.assertIn("已退订", plugin._unsubscribe("s:1"))
            self.assertEqual(plugin.config["subscribed_sessions"], [])
            self.assertEqual(plugin.store.anchors("s:1"), {})

    def test_legacy_subscriptions_migrate_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "subscriptions": {"old:GroupMessage:1": ["featured_scp"]},
                        "latest_by_origin": {"old:GroupMessage:1": {"featured_scp": "featured_scp:x"}},
                    }
                ),
                encoding="utf-8",
            )
            store = AnchorStore(state)
            self.assertEqual(store.pop_legacy_subscriptions(), ["old:GroupMessage:1"])
            self.assertEqual(AnchorStore(state).pop_legacy_subscriptions(), [])
            self.assertEqual(store.anchors("old:GroupMessage:1"), {"featured_scp": "featured_scp:x"})

    def test_schema_matches_code(self):
        schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
        for prefix in ("report", "push"):
            self.assertEqual(schema[f"{prefix}_mode"]["options"], ["image", "markdown", "text"])
            self.assertEqual(schema[f"{prefix}_sections"]["options"], list(SECTIONS))


if __name__ == "__main__":
    unittest.main()
