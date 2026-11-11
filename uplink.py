"""上行转换：MaiBot 入站消息 dict -> gsuid_core MessageReceive.

消息 dict 结构见 MaiBot src/plugin_runtime/host/message_utils.py 的 MessageDict；
段类型映射遵循 gsuid_core 官方适配器指南 §4（text/at/image 上报契约）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Message, MessageReceive
from .permissions import user_pm_for

if TYPE_CHECKING:
    from .plugin import GscoreAdapterPlugin


def extract_text(message: dict[str, Any]) -> str:
    """提取消息纯文本（raw_message 中 text 段拼接；兜底 processed_plain_text）。"""
    parts: list[str] = []
    for seg in message.get("raw_message") or []:
        if isinstance(seg, dict) and seg.get("type") == "text":
            data = seg.get("data")
            if isinstance(data, str):
                parts.append(data)
    if parts:
        return "".join(parts)
    processed = message.get("processed_plain_text")
    return processed if isinstance(processed, str) else ""


def match_prefix(text: str, prefixes: list[str], strict: bool) -> str | None:
    """命中返回前缀，否则 None。

    非严格：以任一前缀开头即命中；严格：前缀后须为空白/结束/非英文字母。
    """
    stripped = text.strip()
    if not stripped:
        return None
    for prefix in prefixes:
        prefix = prefix.strip()
        if not prefix or not stripped.startswith(prefix):
            continue
        if not strict:
            return prefix
        rest = stripped[len(prefix):]
        if rest == "" or rest[0].isspace() or not rest[0].isascii():
            return prefix
    return None


def _at_target(seg: dict[str, Any]) -> str | None:
    """at 段 data 兼容 {"target_user_id": ...} 与裸字符串两种形态。"""
    data = seg.get("data")
    if isinstance(data, dict):
        target = data.get("target_user_id") or data.get("user_id")
        return str(target) if target else None
    if isinstance(data, (str, int)) and str(data):
        return str(data)
    return None


def _image_data(seg: dict[str, Any]) -> str | None:
    """图片段 -> core 可用的 url 或 base64:// 形态（官方红线 15）。"""
    data = seg.get("binary_data_base64")
    if isinstance(data, str) and data.strip():
        return f"base64://{data}"
    raw = seg.get("data")
    if isinstance(raw, str) and raw.startswith(("http://", "https://")):
        return raw
    return None


def build_message_receive(
    plugin: "GscoreAdapterPlugin",
    message: dict[str, Any],
    bot_self_id: str,
) -> MessageReceive | None:
    """把 MaiBot 消息 dict 转换为 MessageReceive；无法构造时返回 None。"""
    info = message.get("message_info") or {}
    user_info = info.get("user_info") or {}
    group_info = info.get("group_info") or {}

    group_id = str(group_info.get("group_id") or "") or None
    user_id = str(user_info.get("user_id") or "")
    if not user_id or user_id == bot_self_id:
        return None  # 无发送者 / bot 自身消息（防回环）

    stream_id = str(message.get("session_id") or "")
    nickname = str(user_info.get("user_nickname") or user_info.get("nickname") or "")
    avatar = str(user_info.get("user_avatar") or user_info.get("avatar") or "")

    content: list[Message] = []
    for seg in message.get("raw_message") or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            data = seg.get("data")
            if isinstance(data, str) and data:
                content.append(Message("text", data))
        elif seg_type in ("image", "emoji"):
            img = _image_data(seg)
            if img:
                content.append(Message("image", img))
        elif seg_type == "at":
            target = _at_target(seg)
            if target:
                # 官方红线 11：at data == bot_self_id 才触发 core 的 is_tome
                content.append(Message("at", bot_self_id if target == bot_self_id else target))
        elif seg_type == "reply":
            # 新版引用契约：reply_id 填被引用消息 id（引用正文 MaiBot 未提供则省略）
            data = seg.get("data")
            reply_id = ""
            if isinstance(data, dict):
                reply_id = str(data.get("message_id") or data.get("target_message_id") or "")
            elif isinstance(data, (str, int)):
                reply_id = str(data)
            if reply_id:
                content.append(Message("reply_id", reply_id))
        elif seg_type == "forward":
            plugin.log("debug", "[GsCore] 上行合并转发暂不支持, 已跳过")
        elif seg_type in ("voice", "record", "file"):
            plugin.log("debug", "[GsCore] 上行段类型 %s 暂不支持, 已跳过", seg_type)

    if not content:
        return None

    # 官方红线 11：群聊 @ 机器人命令需要额外 at 段（私聊 core 自动 is_tome）
    if message.get("is_at") and group_id is not None:
        content.append(Message("at", bot_self_id))

    gs = plugin.config.gscore
    return MessageReceive(
        bot_id=gs.bot_id,
        bot_self_id=bot_self_id,
        msg_id=stream_id,  # core 回包 msg_id 原样带回，用于回溯会话
        user_type="group" if group_id is not None else "direct",
        group_id=group_id,
        user_id=user_id,
        sender={"nickname": nickname, **({"avatar": avatar} if avatar else {})},
        user_pm=user_pm_for(plugin, user_id),
        content=content,
    )
