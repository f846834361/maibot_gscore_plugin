"""core 下发消息段转换工具（官方指南 §5.2/§7：三形态图片、红线 3/14）。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .plugin import GscoreAdapterPlugin

SKIP_TYPES = ("reply", "reply_id", "group", "image_size")
UNSUPPORTED_TYPES = ("record", "video", "file", "buttons", "template_markdown")


def resolve_image(plugin: "GscoreAdapterPlugin", data: str) -> str | None:
    """把 core 图片数据统一解析为 base64（MaiBot send 仅接受 base64）。"""
    if data.startswith("base64://"):
        return data[9:]
    if data.startswith("link://"):
        content = plugin.download_bytes(data[7:])
        if content:
            return base64.b64encode(content).decode("utf-8")
        plugin.log("warning", "[GsCore] link:// 图片下载失败, 已跳过")
        return None
    if data.startswith("file://"):
        content = plugin.read_local_file(data[7:])
        if content:
            return base64.b64encode(content).decode("utf-8")
        plugin.log("warning", "[GsCore] file:// 图片在本机不可读, 已跳过")
        return None
    plugin.log("warning", "[GsCore] 未识别的图片形态, 已跳过")
    return None


def build_segments(plugin: "GscoreAdapterPlugin", content: list[Any]) -> tuple[str, list[str]]:
    """把 core content 段列表转为 (合并文本, base64图片列表)。

    at/markdown 降级为文本；不支持与未知的段类型按红线 14 跳过。
    """
    text_parts: list[str] = []
    images: list[str] = []
    for seg in content:
        seg_type = seg.type
        data = seg.data
        if not data:
            continue
        if seg_type == "text":
            text_parts.append(str(data))
        elif seg_type == "image":
            b64 = resolve_image(plugin, str(data))
            if b64:
                images.append(b64)
        elif seg_type == "at":
            text_parts.append(f"@{data} ")
        elif seg_type == "markdown":
            text_parts.append(str(data))
        elif seg_type in UNSUPPORTED_TYPES:
            plugin.log("warning", "[GsCore] 暂不支持下发段类型 %s, 已跳过", seg_type)
        elif seg_type not in SKIP_TYPES:
            plugin.log("warning", "[GsCore] 未知下发段类型 %s, 已跳过", seg_type)
    return "".join(text_parts), images


def to_hybrid_segments(text: str, images: list[str]) -> list[dict[str, Any]]:
    """构造 ctx.send.hybrid 的 segments（text + image）。"""
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": text})
    for b64 in images:
        segments.append({"type": "image", "binary_data_base64": b64})
    return segments
