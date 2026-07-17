import asyncio
import json
import sys
import tempfile
import types
import unittest
from xml.etree import ElementTree
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Filter:
    @staticmethod
    def on_astrbot_loaded():
        return lambda function: function

    @staticmethod
    def command_group(_name):
        def decorator(function):
            function.command = lambda _command: lambda handler: handler
            return function

        return decorator


class _MessageChain:
    def __init__(self, components=None):
        self.components = list(components or [])

    def message(self, value):
        self.components.append(_Plain(value))
        return self


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    @classmethod
    def fromFileSystem(cls, path):
        image = cls()
        image.path = path
        return image


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = _Logger()
event_api = types.ModuleType("astrbot.api.event")
event_api.AstrMessageEvent = object
event_api.MessageChain = _MessageChain
event_api.filter = _Filter()
components_api = types.ModuleType("astrbot.api.message_components")
components_api.Image = _Image
components_api.Plain = _Plain
star_api = types.ModuleType("astrbot.api.star")
star_api.Context = object
star_api.Star = object
star_api.StarTools = type("StarTools", (), {"get_data_dir": staticmethod(lambda _name: Path("."))})
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)
sys.modules.setdefault("astrbot.api.event", event_api)
sys.modules.setdefault("astrbot.api.message_components", components_api)
sys.modules.setdefault("astrbot.api.star", star_api)

try:
    import httpx  # noqa: F401
except ImportError:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = object
    sys.modules["httpx"] = httpx_stub

try:
    import defusedxml  # noqa: F401
except ImportError:
    defusedxml_stub = types.ModuleType("defusedxml")
    defusedxml_stub.DefusedXmlException = ElementTree.ParseError
    defusedxml_element_tree_stub = types.ModuleType("defusedxml.ElementTree")
    defusedxml_element_tree_stub.fromstring = ElementTree.fromstring
    sys.modules["defusedxml"] = defusedxml_stub
    sys.modules["defusedxml.ElementTree"] = defusedxml_element_tree_stub

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_scp_cn_feed import main as main_module  # noqa: E402
from astrbot_plugin_scp_cn_feed.main import (  # noqa: E402
    PUSH_MODE_DAILY_REPORT,
    PUSH_MODE_MODULE_SCREENSHOT,
    PUSH_MODE_TEXT,
    ScpCnFeedPlugin,
)
from astrbot_plugin_scp_cn_feed.scp_cn_feed.models import FeedItem, SOURCES  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.renderer import (  # noqa: E402
    FeedRenderer,
    SCP_FOUNDATION_LOGO_URL,
)
from astrbot_plugin_scp_cn_feed.scp_cn_feed.service import FeedService  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.storage import FeedStore  # noqa: E402
from astrbot_plugin_scp_cn_feed.scp_cn_feed.wikidot_api import WikidotApiError  # noqa: E402


class _Config(dict):
    def __init__(self, **values):
        super().__init__(**values)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


def _plugin(config=None):
    plugin = object.__new__(ScpCnFeedPlugin)
    plugin.config = config if config is not None else _Config()
    return plugin


class SubscriptionConfigTests(unittest.TestCase):
    def test_command_state_is_written_as_editable_template_entries(self):
        config = _Config(subscription_sessions=[])
        plugin = _plugin(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin.store = FeedStore(Path(temp_dir) / "state.json")
            plugin.store.replace_subscriptions(
                {"aiocqhttp:GroupMessage:123": {"featured_scp", "contests"}}
            )

            self.assertTrue(plugin._sync_subscription_config_from_store())

        self.assertEqual(config.save_count, 1)
        self.assertEqual(
            config["subscription_sessions"],
            [
                {
                    "__template_key": "subscription",
                    "origin": "aiocqhttp:GroupMessage:123",
                    "featured_scp": True,
                    "featured_tale": False,
                    "contests": True,
                }
            ],
        )

    def test_config_edits_replace_state_and_cleanup_removed_anchors(self):
        config = _Config(
            subscription_sessions=[
                {
                    "__template_key": "subscription",
                    "origin": "group:2",
                    "featured_scp": False,
                    "featured_tale": True,
                    "contests": False,
                }
            ]
        )
        plugin = _plugin(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin.store = FeedStore(Path(temp_dir) / "state.json")
            plugin.store.replace_subscriptions({"group:1": {"featured_scp"}})
            plugin.store.mark_latest("group:1", "featured_scp", "featured_scp:old")

            plugin._sync_subscriptions_from_config()

            self.assertEqual(plugin.store.all_subscriptions(), {"group:2": {"featured_tale"}})
            self.assertIsNone(plugin.store.latest_item_id("group:1", "featured_scp"))

    def test_legacy_state_is_migrated_to_config_once(self):
        config = _Config(subscription_sessions=[])
        plugin = _plugin(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin.store = FeedStore(Path(temp_dir) / "state.json")
            plugin.store.subscribe("legacy:group", "featured_scp")

            plugin._initialize_subscription_config()

            self.assertTrue(plugin.store.subscription_config_sync_complete())
            self.assertEqual(config["subscription_sessions"][0]["origin"], "legacy:group")
            self.assertEqual(config.save_count, 1)


class PushModeTests(unittest.TestCase):
    def test_all_three_push_modes_and_invalid_fallback(self):
        plugin = _plugin(_Config(update_push_mode=PUSH_MODE_DAILY_REPORT))
        self.assertEqual(plugin._update_push_mode(), PUSH_MODE_DAILY_REPORT)
        plugin.config["update_push_mode"] = PUSH_MODE_MODULE_SCREENSHOT
        self.assertEqual(plugin._update_push_mode(), PUSH_MODE_MODULE_SCREENSHOT)
        plugin.config["update_push_mode"] = PUSH_MODE_TEXT
        self.assertEqual(plugin._update_push_mode(), PUSH_MODE_TEXT)
        plugin.config["update_push_mode"] = "broken"
        self.assertEqual(plugin._update_push_mode(), PUSH_MODE_MODULE_SCREENSHOT)

    def test_source_fetch_failures_are_isolated(self):
        plugin = _plugin()

        class Service:
            async def fetch_source(self, source, limit, use_cache):
                if source.key == "featured_tale":
                    raise WikidotApiError("temporary failure")
                return [
                    FeedItem(
                        source_key=source.key,
                        fullname=f"{source.key}-1",
                        title=source.title,
                        url="https://example.invalid/item",
                    )
                ]

        plugin.service = Service()
        fetched, errors = asyncio.run(
            plugin._fetch_sources_safely(
                {"featured_scp", "featured_tale"},
                limit=5,
            )
        )
        self.assertIn("featured_scp", fetched)
        self.assertNotIn("featured_tale", fetched)
        self.assertEqual(errors, {"featured_tale": "temporary failure"})

    def test_poll_dispatches_each_selected_push_mode(self):
        class Client:
            async def fetch_source(self, source, limit, use_cache):
                if source.key == "featured_scp":
                    return [
                        FeedItem(
                            source_key=source.key,
                            fullname="new-item",
                            title="新增项目",
                            url="https://example.invalid/new",
                        ),
                        FeedItem(
                            source_key=source.key,
                            fullname="old-item",
                            title="旧项目",
                            url="https://example.invalid/old",
                        ),
                    ]
                return [
                    FeedItem(
                        source_key=source.key,
                        fullname=f"{source.key}-current",
                        title=source.title,
                        url=f"https://example.invalid/{source.key}",
                    )
                ]

        class Renderer:
            def prune_old_files(self):
                pass

            async def render_update_screenshot(self, source, items):
                return Path(f"{source.key}.png")

        class Context:
            def __init__(self):
                self.sent = []

            async def send_message(self, origin, chain):
                self.sent.append((origin, chain))

        expectations = {
            PUSH_MODE_DAILY_REPORT: "SCP-CN 日报",
            PUSH_MODE_MODULE_SCREENSHOT: "SCP-CN 精品 SCP：",
            PUSH_MODE_TEXT: "[SCP-CN 精品 SCP更新]",
        }
        original_interval = main_module.PUSH_SEND_INTERVAL_SECONDS
        main_module.PUSH_SEND_INTERVAL_SECONDS = 0
        try:
            for mode, expected_text in expectations.items():
                with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                    config = _Config(
                        update_push_mode=mode,
                        enable_daily_report_image=False,
                        subscription_sessions=[
                            {
                                "__template_key": "subscription",
                                "origin": "group:1",
                                "featured_scp": True,
                                "featured_tale": False,
                                "contests": False,
                            }
                        ],
                    )
                    plugin = _plugin(config)
                    plugin.store = FeedStore(Path(temp_dir) / "state.json")
                    plugin.store.replace_subscriptions({"group:1": {"featured_scp"}})
                    plugin.store.mark_latest(
                        "group:1",
                        "featured_scp",
                        "featured_scp:old-item",
                    )
                    plugin.service = FeedService(plugin.store, Client())
                    plugin.renderer = Renderer()
                    plugin.context = Context()

                    asyncio.run(plugin._poll_once())

                    self.assertEqual(len(plugin.context.sent), 1)
                    _origin, chain = plugin.context.sent[0]
                    plain_text = "".join(
                        component.text
                        for component in chain.components
                        if isinstance(component, _Plain)
                    )
                    self.assertIn(expected_text, plain_text)
                    self.assertEqual(
                        plugin.store.latest_item_id("group:1", "featured_scp"),
                        "featured_scp:new-item",
                    )
        finally:
            main_module.PUSH_SEND_INTERVAL_SECONDS = original_interval


class DailyReportRenderTests(unittest.TestCase):
    def test_daily_html_contains_foundation_logo_and_expected_colors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = FeedRenderer(Path(temp_dir))
            item = FeedItem(
                source_key="featured_scp",
                fullname="scp-cn-test",
                title="测试项目",
                url="https://scp-wiki-cn.wikidot.com/scp-cn-test",
                created_by="作者甲",
                summary="正文链接",
                summary_html='<span class="summary-link">正文链接</span>',
            )
            html_text = renderer._build_daily_html(
                {"featured_scp": [item], "featured_tale": [], "contests": []},
                {},
                ("featured_scp", "featured_tale", "contests"),
                SOURCES,
            )

        self.assertIn(SCP_FOUNDATION_LOGO_URL, html_text)
        self.assertIn(".meta .label", html_text)
        self.assertIn("color: #2367a5", html_text)
        self.assertIn(".summary-link", html_text)
        self.assertIn("color: #b3272d", html_text)

    def test_schema_exposes_subscription_editor_and_exactly_three_push_choices(self):
        schema = json.loads(
            (Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["subscription_sessions"]["type"], "template_list")
        self.assertEqual(
            schema["update_push_mode"]["options"],
            [PUSH_MODE_DAILY_REPORT, PUSH_MODE_MODULE_SCREENSHOT, PUSH_MODE_TEXT],
        )


if __name__ == "__main__":
    unittest.main()
