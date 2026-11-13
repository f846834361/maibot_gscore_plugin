"""与 gsuid_core 的 WebSocket 连接客户端（连接生命周期，协议侧不含平台逻辑）.

职责（对齐官方适配器开发指南 §3 与 astrbot_plugin_gscore_adapter/client.py）：
- 维护唯一 WS 连接：建连 / 断线自动重连（带退避与限次）/ 优雅关闭；
- 上行队列串行化发送（二进制帧），断连期间暂存、重连后补发；
- 下发帧原样交给插件分发器处理（含 log 回显包 / 控制包 / 普通消息）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp

from .models import MessageReceive, encode_message_receive

if TYPE_CHECKING:
    from .plugin import GscoreAdapterPlugin

# 官方指南红线 6：max_size 必须调大（base64 图片轻松超 1MB），对齐官方 2**26
WS_MAX_MSG_SIZE = 2**26
WS_HEARTBEAT = 30.0


class GsClient:
    """维护与 gsuid_core 的 WS 连接：上报队列 + 下发分发 + 断线重连."""

    def __init__(self, plugin: "GscoreAdapterPlugin") -> None:
        self._plugin = plugin
        self._queue: asyncio.Queue[MessageReceive] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._retry = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def retry_count(self) -> int:
        return self._retry

    def _conn_cfg(self) -> dict[str, Any]:
        gs = self._plugin.config.gscore
        return {
            "host": gs.host,
            "port": gs.port,
            "scheme": gs.scheme,
            "route_id": gs.route_id,
            "ws_token": gs.ws_token,
            "interval": gs.reconnect_interval,
            "max_retry": gs.max_retry,
        }

    def ws_url(self) -> str:
        cfg = self._conn_cfg()
        url = f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}/ws/{cfg['route_id']}"
        if cfg["ws_token"]:
            url += f"?token={cfg['ws_token']}"
        return url

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动（或拉起已退出的）连接主循环；幂等。"""
        if self.is_running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="gscore-plugin-ws")

    async def stop(self) -> None:
        """停止连接主循环并关闭连接（插件卸载 / 重载时调用）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close()

    async def restart(self) -> None:
        """配置变更后重启连接。"""
        await self.stop()
        self._retry = 0
        await self.start()

    async def _close(self) -> None:
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with suppress(Exception):
                await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------
    async def report(self, msg: MessageReceive) -> None:
        """上报消息入队；断连期间暂存，重连后由发送循环补发。"""
        if not self.is_running:
            self._plugin.log("warning", "[GsCore] 尚未连接, 上报消息丢弃")
            return
        await self._queue.put(msg)

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while self._running:
            cfg = self._conn_cfg()
            try:
                ws = await self._connect(cfg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 连接失败原因多样，统一退避重试
                self._retry += 1
                max_retry = int(cfg["max_retry"] or 0)
                if max_retry > 0 and self._retry > max_retry:
                    self._plugin.log(
                        "error",
                        "[GsCore] 已连续 %d 次连接失败, 停止重试; 修改配置或发送 /连接core 可重新连接",
                        max_retry,
                    )
                    break
                self._plugin.log(
                    "warning",
                    "[链接错误] Core服务器连接失败: %s ...请确认早柚核心(gsuid_core)已启动"
                    "且 HOST/PORT 正确! %s 秒后重试 (第 %d 次)",
                    e,
                    cfg["interval"],
                    self._retry,
                )
                await asyncio.sleep(max(1, int(cfg["interval"])))
                continue
            self._retry = 0
            await self._serve(ws)
            if not self._running:
                break
            self._plugin.log("warning", "与[gsuid-core]断开连接! 路由ID: %s", cfg["route_id"])
            await asyncio.sleep(max(1, int(cfg["interval"])))

    async def _connect(self, cfg: dict[str, Any]) -> aiohttp.ClientWebSocketResponse:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = self.ws_url()
        self._plugin.log("info", "路由ID %s 连接至[gsuid-core]: %s", cfg["route_id"], url)
        ws = await self._session.ws_connect(
            url,
            max_msg_size=WS_MAX_MSG_SIZE,
            heartbeat=WS_HEARTBEAT,
        )
        self._ws = ws
        self._plugin.log("info", "与[gsuid-core]成功连接! 路由ID: %s", cfg["route_id"])
        return ws

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """收包主循环；返回即连接断开。单包异常不影响整体循环（红线 9）。"""
        send_task = asyncio.create_task(self._send_loop(ws))
        try:
            async for raw in ws:
                if raw.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    continue
                try:
                    await self._plugin.handle_core_packet(raw.data)
                except Exception as e:  # noqa: BLE001
                    self._plugin.log("error", "[GsCore] 处理下发包失败: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 连接级异常交给监督循环重连
            self._plugin.log("warning", "[GsCore] 收包循环异常退出: %s", e)
        finally:
            send_task.cancel()
            with suppress(asyncio.CancelledError):
                await send_task

    async def _send_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """上报队列串行化发送（红线 8）；发送失败放回队列待重连后补发。"""
        while True:
            msg = await self._queue.get()
            try:
                await ws.send_bytes(encode_message_receive(msg))
            except Exception:  # noqa: BLE001
                self._queue.put_nowait(msg)
                return
