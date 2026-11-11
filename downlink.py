"""下行分发：core 下发的 MessageSend -> MaiBot 会话消息（官方指南 §5/§11）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Message, MessageReceive, MessageSend, decode_message_send
from .segments import build_segments, to_hybrid_segments

if TYPE_CHECKING:
    from .plugin import GscoreAdapterPlugin


async def _resolve_stream_id(plugin: "GscoreAdapterPlugin", msg: MessageSend) -> str | None:
    """定位会话：优先用上报时缓存的 stream_id（msg_id 回填），再按群号/用户号兜底。"""
    stream_id = plugin.lookup_stream_by_msg_id(msg.msg_id)
    if stream_id:
        return stream_id
    target_id = str(msg.target_id or "")
    if not target_id:
        return None
    if msg.target_type == "group":
        return await plugin.find_group_stream(target_id)
    return await plugin.find_private_stream(target_id)


async def _send_one(
    plugin: "GscoreAdapterPlugin", stream_id: str, text: str, images: list[str]
) -> str | None:
    """发送一次消息（无内容则跳过），返回平台出站 message_id。"""
    if not text and not images:
        return None
    send = plugin.ctx.send
    if images:
        result = await send.hybrid(
            to_hybrid_segments(text, images), stream_id, return_details=True
        )
    else:
        result = await send.text(text, stream_id, return_details=True)
    if isinstance(result, dict):
        return result.get("message_id") or None
    return None


async def _dispatch_content(plugin: "GscoreAdapterPlugin", stream_id: str, msg: MessageSend) -> str | None:
    """逐段分发；node 拆条发送（无原生合并转发能力，官方标准做法）。"""
    ids: list[str] = []
    for seg in msg.content:
        if seg.type == "node" and isinstance(seg.data, list):
            for node in seg.data:
                if not isinstance(node, dict):
                    continue
                fake = Message(type=node.get("type"), data=node.get("data"))
                text, images = build_segments(plugin, [fake])
                mid = await _send_one(plugin, stream_id, text, images)
                if mid:
                    ids.append(mid)
        else:
            text, images = build_segments(plugin, [seg])
            mid = await _send_one(plugin, stream_id, text, images)
            if mid:
                ids.append(mid)
    if not ids:
        return None
    return ids[0] if len(ids) == 1 else ",".join(ids)


async def _send_echo_receipt(plugin: "GscoreAdapterPlugin", msg: MessageSend, recall_id: str) -> None:
    """echo 非空时回执 recall_message_id（官方红线 18）。"""
    gs = plugin.config.gscore
    await plugin.client.report(
        MessageReceive(
            bot_id=gs.bot_id,
            bot_self_id=msg.bot_self_id,
            msg_id=msg.msg_id,
            user_type=msg.target_type or "group",
            group_id=msg.target_id if msg.target_type == "group" else None,
            content=[Message("recall_message_id", {"echo": msg.echo, "id": recall_id})],
        )
    )


async def handle_core_packet(plugin: "GscoreAdapterPlugin", raw: Any) -> None:
    """下发帧入口：路由 -> log 包 / 控制包 / 普通消息。"""
    msg = decode_message_send(raw)
    gs = plugin.config.gscore

    # ① 日志回显包（bot_id == 连接路由 ID，红线 4）
    if msg.bot_id == gs.route_id and msg.is_log:
        level = (msg.content[0].type or "log_INFO").split("_")[-1].lower()
        level = {"success": "info", "info": "info", "warning": "warning", "error": "error"}.get(
            level, "info"
        )
        plugin.log(level, "[GsCore 日志] %s", msg.content[0].data)
        return

    # ② 平台路由校验（红线 2：上报/下发用同一平台 bot_id，含 ":" 变体）
    if not (msg.bot_id == gs.bot_id or msg.bot_id.startswith(f"{gs.bot_id}:")):
        plugin.log("debug", "[GsCore] 忽略其他平台下发包 bot_id=%s", msg.bot_id)
        return

    # ③ 控制包短路（红线 16）：暂不支持撤回/禁言，避免误发空消息
    if len(msg.content) == 1 and msg.content[0].type == "excute_delete_message":
        plugin.log("warning", "[GsCore] MaiBot 侧暂不支持撤回消息, 已忽略")
        return

    # ④ 普通消息分发
    plugin.log(
        "info", "【接收】[gsuid-core]: %s - %s - %s", msg.bot_id, msg.target_type, msg.target_id
    )
    stream_id = await _resolve_stream_id(plugin, msg)
    if not stream_id:
        plugin.log("warning", "[GsCore] 无法定位目标会话, 丢弃下发消息")
        return

    recall_id: str | None = None
    try:
        recall_id = await _dispatch_content(plugin, stream_id, msg)
    except Exception as e:  # noqa: BLE001
        plugin.log("error", "[GsCore] 发送消息失败: %s", e)
        return
    if msg.echo and recall_id:
        await _send_echo_receipt(plugin, msg, recall_id)
