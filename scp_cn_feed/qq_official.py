"""绕开 AstrBot 的会话缓存，直接调 QQ 开放平台接口发消息。

AstrBot 的 qq_official 适配器只在内存里记住群场景和最近的 msg_id，
重启或长时间没人 @ 机器人后，定时推送会被静默跳过；Markdown 按钮也没法通过消息链发送。
"""

from __future__ import annotations

import base64
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


QQ_OFFICIAL_ADAPTERS = {"qq_official", "qq_official_webhook"}


@dataclass(frozen=True)
class QQOfficialTarget:
    api: Any
    scene: str
    openid: str
    msg_id: str | None = None

    @classmethod
    def resolve(cls, platform: Any, origin: str, msg_id: str | None = None) -> QQOfficialTarget | None:
        """群聊和单聊返回发送目标；频道或其他平台返回 None，交给 AstrBot 默认发送。"""
        if platform is None or platform.meta().name not in QQ_OFFICIAL_ADAPTERS:
            return None
        _, message_type, session_id = origin.split(":", 2)
        scenes = {"GroupMessage": "group", "FriendMessage": "c2c"}
        # 群聊开了会话隔离时 session_id 形如 “用户_群”；频道和频道私信的 ID 是纯数字。
        openid = session_id.rsplit("_", 1)[-1]
        if message_type not in scenes or openid.isdigit():
            return None
        return cls(platform.get_client().api, scenes[message_type], openid, msg_id)

    async def send_markdown(self, content: str, keyboard: dict | None = None) -> None:
        payload: dict[str, Any] = {"msg_type": 2, "markdown": {"content": content}}
        if keyboard:
            payload["keyboard"] = keyboard
        await self._post(payload)

    async def send_image(self, path: Path) -> None:
        from botpy.http import Route

        if self.scene == "group":
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=self.openid)
        else:
            route = Route("POST", "/v2/users/{openid}/files", openid=self.openid)
        media = await self.api._http.request(
            route,
            json={
                "file_type": 1,
                "file_data": base64.b64encode(path.read_bytes()).decode(),
                "srv_send_msg": False,
            },
        )
        await self._post({"msg_type": 7, "media": {"file_info": media["file_info"]}})

    async def _post(self, payload: dict[str, Any]) -> None:
        payload["msg_seq"] = random.randint(1, 2**31 - 1)
        if self.msg_id:
            payload["msg_id"] = self.msg_id
        if self.scene == "group":
            await self.api.post_group_message(group_openid=self.openid, **payload)
        else:
            await self.api.post_c2c_message(openid=self.openid, **payload)
