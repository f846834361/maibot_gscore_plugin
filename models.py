"""gsuid_core（早柚核心）协议数据模型.

与 core 侧 `gsuid_core/models.py` 的 msgspec Struct 保持字段级一致，
用标准库 dataclass + json 实现（MaiBot 运行环境无 msgspec 依赖）。

参考：gsuid_core/.agents/skills/gscore-adapter-development/references/02-data-structures.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """消息段：type 为段类型（text/image/at/...），data 随 type 而变。"""

    type: str | None = None
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, item: Any) -> "Message":
        if isinstance(item, Message):
            return item
        if isinstance(item, dict):
            return cls(type=item.get("type"), data=item.get("data"))
        return cls()


@dataclass
class MessageReceive:
    """上报包（平台 -> core）。

    - bot_id: 平台 ID（决定 core 内部渲染与下发路由，上报/下发必须一致）
    - bot_self_id: 机器人账号 ID（QQ 号）
    - msg_id: 平台侧消息 ID，此处填 MaiBot 的 stream_id，
      core 下发时会原样回填到 MessageSend.msg_id，适配器据此回溯原会话
    - user_type: group / direct
    - user_pm: 用户权限，越小越高（群主 2 / 管理 3 / 普通 6，禁止 0）
    """

    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: str = "group"
    group_id: str | None = None
    user_id: str | None = None
    sender: dict[str, Any] = field(default_factory=dict)
    user_pm: int = 6
    content: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "bot_self_id": self.bot_self_id,
            "msg_id": self.msg_id,
            "user_type": self.user_type,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "sender": self.sender,
            "user_pm": self.user_pm,
            "content": [seg.to_dict() for seg in self.content],
        }


@dataclass
class MessageSend:
    """下发包（core -> 平台）。"""

    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: str | None = None
    target_id: str | None = None
    content: list[Message] = field(default_factory=list)
    echo: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "MessageSend":
        """从 json 解析结果构造；未知字段自动忽略，缺失字段用默认值。"""
        if not isinstance(raw, dict):
            raise TypeError(f"MessageSend 期望 dict, 收到 {type(raw).__name__}")
        content_raw = raw.get("content") or []
        content = [Message.from_dict(item) for item in content_raw]
        return cls(
            bot_id=str(raw.get("bot_id") or "Bot"),
            bot_self_id=str(raw.get("bot_self_id") or ""),
            msg_id=str(raw.get("msg_id") or ""),
            target_type=raw.get("target_type"),
            target_id=raw.get("target_id"),
            content=content,
            echo=raw.get("echo"),
        )

    @property
    def is_log(self) -> bool:
        """log_{LEVEL} 日志回显包：首段 type 以 log 开头。"""
        if not self.content:
            return False
        seg_type = self.content[0].type or ""
        return seg_type.startswith("log")


def encode_message_receive(msg: MessageReceive) -> bytes:
    """序列化为二进制帧载荷（core 端 receive_bytes，禁止发文本帧）。"""
    return json.dumps(msg.to_dict(), ensure_ascii=False).encode("utf-8")


def decode_message_send(raw: bytes | str) -> MessageSend:
    """解析 core 下发的二进制帧 / 文本。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return MessageSend.from_raw(json.loads(raw))
