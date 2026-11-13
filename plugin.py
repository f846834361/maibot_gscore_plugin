"""maibot_gscore_plugin - MaiBot 对接 gsuid_core（早柚核心）插件.

把命中前缀（如 gs/sr/zzz）的 MaiBot 消息经 WebSocket 转发给 gsuid_core，
并把 core 的回复（文本/图片等）发回原 QQ 会话；命中消息不会进入 LLM 闲聊链路。
"""

from __future__ import annotations

from typing import Any, Literal

import aiohttp

from maibot_sdk import (
    Command,
    ConfigWidget,
    ConfigWidgetState,
    Field,
    HookHandler,
    HookResult,
    MaiBotPlugin,
    PluginConfigBase,
    CONFIG_RELOAD_SCOPE_SELF,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .downlink import handle_core_packet
from .gs_client import GsClient
from .permissions import check_forward, is_admin
from .uplink import build_message_receive, extract_text, match_prefix

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class PluginSection(PluginConfigBase):
    """插件级配置."""

    __ui_label__ = "基础"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置版本（由插件维护，一般无需修改）",
        json_schema_extra={"label": "配置版本"},
    )
    enabled: bool = Field(
        default=True,
        description="关闭后插件完全放行所有消息，Hook 不再拦截",
        json_schema_extra={"label": "启用转发"},
    )


class GsCoreSettings(PluginConfigBase):
    """gsuid_core 连接与转发配置."""

    __ui_label__ = "GsCore 连接"
    __ui_order__ = 1

    host: str = Field(
        default="127.0.0.1",
        description="gsuid_core WebSocket 服务地址（core 与 MaiBot 同机填 127.0.0.1，Docker 部署时填容器可达地址）",
        json_schema_extra={"label": "服务地址"},
    )
    port: int = Field(
        default=8765,
        description="gsuid_core WebSocket 服务端口",
        ge=1,
        le=65535,
        json_schema_extra={"label": "服务端口"},
    )
    route_id: str = Field(
        default="MaiBot",
        description="连接路由 ID（/ws/{route_id}），同一 core 可被多个机器人以不同路由复用；日志回显包的 bot_id 即为该值",
        json_schema_extra={"label": "连接路由 ID"},
    )
    bot_id: str = Field(
        default="maibot",
        description="平台 ID（广告位/渲染/回包路由标识），必须与 core 配置中的平台名一致",
        json_schema_extra={"label": "平台 ID"},
    )
    bot_self_id: str = Field(
        default="",
        description="机器人账号 ID（QQ 号），留空则自动取消息中的 bot 账号",
        json_schema_extra={"label": "机器人账号"},
    )
    scheme: Literal["ws", "wss"] = Field(
        default="ws",
        description="连接协议：ws 明文 / wss TLS 加密（wss 需服务器域名配有有效证书，如 Nginx 反代 + Let's Encrypt）",
        json_schema_extra={"label": "连接协议"},
    )
    ws_token: str = Field(
        default="",
        description="core 侧 [bot平台配置] 的 token，未设置鉴权时留空",
        json_schema_extra={"label": "连接 Token", "input_type": "password"},
    )
    reconnect_interval: int = Field(
        default=5,
        description="断线重连间隔（秒）",
        ge=1,
        le=3600,
        json_schema_extra={"label": "重连间隔（秒）"},
    )
    max_retry: int = Field(
        default=5,
        description="连续连接失败的最大重试次数，0 表示无限重试；配置变更会重置计数并重新连接",
        ge=0,
        le=10000,
        json_schema_extra={"label": "最大重试次数"},
    )
    only_prefixes: list[str] = Field(
        default_factory=lambda: ["gs", "sr", "zzz", "ww", "mys"],
        description="触发转发的命令前缀列表（gsuid_core 已启用的适配器前缀）",
        json_schema_extra={"label": "触发前缀", "x-widget": "tags"},
    )
    strict_prefix: bool = Field(
        default=False,
        description="前缀后必须跟空白或结束（避免英文单词误判，如 gserver）",
        json_schema_extra={"label": "严格前缀模式"},
    )
    forward_private: bool = Field(
        default=True,
        description="私聊消息是否免前缀全量转发",
        json_schema_extra={"label": "私聊免前缀转发"},
    )
    platforms: list[Literal["qq", "telegram", "weixin_clawbot", "internal"]] = Field(
        default_factory=lambda: ["qq"],
        description="需要监听的平台（多选下拉）：qq / telegram / weixin_clawbot / internal(WebUI 聊天室)；留空表示不限平台。第三方适配器的自定义平台名请填在 custom_platforms",
        json_schema_extra={"label": "监听平台"},
    )
    custom_platforms: list[str] = Field(
        default_factory=list,
        description="自定义平台名（第三方适配器声明的 platform 值）；与上面多选结果合并，两者都为空表示不限平台",
        json_schema_extra={
            "label": "自定义平台名",
            "x-widget": "tags",
            "placeholder": "输入平台名后回车",
        },
    )
    user_pm: int = Field(
        default=6,
        description="上报 core 的用户权限等级（0 禁止 / 2 群主 / 3 管理 / 6 普通）",
        ge=0,
        le=6,
        json_schema_extra={"label": "上报用户权限"},
    )


class PermissionSettings(PluginConfigBase):
    """权限控制配置（黑白名单与管理员）."""

    __ui_label__ = "权限控制"
    __ui_order__ = 2

    admin_users: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表：可使用 /连接core 等管理命令，上报 core 的权限至少为管理（pm=3）；留空表示不限制管理命令",
        json_schema_extra={"label": "管理员 QQ", "x-widget": "tags"},
    )
    blocked_users: list[str] = Field(
        default_factory=list,
        description="屏蔽 QQ 号列表：这些用户的消息一律不转发（最高优先级，私聊群聊都拒）",
        json_schema_extra={"label": "屏蔽 QQ", "x-widget": "tags"},
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群聊白名单（群号列表）：留空表示所有群可用；与黑名单同时配置时黑名单优先",
        json_schema_extra={"label": "群聊白名单", "x-widget": "tags"},
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        description="群聊黑名单（群号列表）：命中的群一律不转发",
        json_schema_extra={"label": "群聊黑名单", "x-widget": "tags"},
    )
    private_whitelist: list[str] = Field(
        default_factory=list,
        description="私聊白名单（QQ 号列表）：留空表示所有私聊可用；与黑名单同时配置时黑名单优先",
        json_schema_extra={"label": "私聊白名单", "x-widget": "tags"},
    )
    private_blacklist: list[str] = Field(
        default_factory=list,
        description="私聊黑名单（QQ 号列表）：命中的用户私聊一律不转发",
        json_schema_extra={"label": "私聊黑名单", "x-widget": "tags"},
    )


class GscoreAdapterConfig(PluginConfigBase):
    """插件根配置模型."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    gscore: GsCoreSettings = Field(default_factory=GsCoreSettings)
    permission: PermissionSettings = Field(default_factory=PermissionSettings)


class GscoreAdapterPlugin(MaiBotPlugin):
    """gsuid_core 适配插件：上行转发 + 下行分发 + 连接生命周期管理."""

    config_model = GscoreAdapterConfig
    config_widgets = [ConfigWidget(id="gscore_status", kind="status", label="GsCore 连接")]

    def __init__(self) -> None:
        super().__init__()
        self._client: GsClient | None = None
        self._msg_stream_map: dict[str, str] = {}  # 上报 msg_id -> stream_id
        self._stream_cache: dict[tuple[str, str], str] = {}  # (group_id,user_id) -> stream_id

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        self._client = GsClient(self)
        if self.config.plugin.enabled:
            await self._client.start()
            await self.check_platform_config()
        self.log("info", "GsCore 适配器插件已加载: %s", self._client.ws_url() if self._client else "")

    async def on_unload(self) -> None:
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._msg_stream_map.clear()
        self._stream_cache.clear()
        self.log("info", "GsCore 适配器插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        # 事件驱动入口：宿主 apply_plugin_config → RPC plugin.config_updated → 到达这里。
        # 页面保存后若始终看不到本日志，说明宿主侧（文件监听→apply）未触发。
        self.log("info", "[GsCore] 收到配置更新事件: scope=%s, version=%s", scope, version)
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            self.log("info", "[GsCore] 非 self 范围的配置更新, 忽略 (scope=%s)", scope)
            return
        await self._apply_runtime_config()

    async def _apply_runtime_config(self) -> None:
        """把当前内存配置应用到运行时（重连或停止）；配置热更新事件触发。"""
        cfg = self.config
        gs = cfg.gscore
        self.log(
            "info",
            "[GsCore] 应用配置: enabled=%s scheme=%s host=%s port=%s route_id=%s max_retry=%s",
            cfg.plugin.enabled, gs.scheme, gs.host, gs.port, gs.route_id, gs.max_retry,
        )
        if not cfg.plugin.enabled:
            if self._client is not None:
                await self._client.stop()
            self.log("info", "[GsCore] 插件已停用, 连接已停止")
            return
        if self._client is None:
            self._client = GsClient(self)
        await self._client.restart()
        await self.check_platform_config()
        self.log("info", "GsCore 配置已热重载并重连")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def log(self, level: str, msg: str, *args: Any) -> None:
        getattr(self.ctx.logger, level if level != "warning" else "warning")(msg, *args)

    def target_platforms(self) -> set[str]:
        """生效的监听平台集合（统一小写去空白）；空集合表示不限平台。"""
        gs = self.config.gscore
        raw = [*gs.platforms, *gs.custom_platforms]
        return {str(item).strip().lower() for item in raw if str(item).strip()}

    async def probe_platforms(self) -> set[str]:
        """枚举当前真实在线的平台名（chat.get_all_streams 的 all_platforms 特值）。"""
        result = await self.ctx.chat.get_all_streams(platform="all_platforms")
        streams = result.get("streams") if isinstance(result, dict) else result
        online: set[str] = set()
        for stream in streams or []:
            if isinstance(stream, dict):
                name = str(stream.get("platform") or "").strip().lower()
                if name:
                    online.add(name)
        return online

    async def check_platform_config(self) -> None:
        """启动/热重载时核对平台配置与实际在线平台，避免拼写错误静默失效。"""
        targets = self.target_platforms()
        try:
            online = await self.probe_platforms()
        except Exception as e:  # noqa: BLE001
            self.log("debug", "[GsCore] 平台探测失败（不影响转发）: %s", e)
            return
        if not online:
            self.log("debug", "[GsCore] 暂无在线会话，跳过平台配置核对")
            return
        self.log("info", "[GsCore] 当前在线平台: %s", ", ".join(sorted(online)))
        unknown = targets - online
        if unknown:
            self.log(
                "warning",
                "[GsCore] 监听平台 %s 当前没有会话（在线: %s），这些平台的消息不会转发；请核对适配器实际上报的 platform 值",
                ", ".join(sorted(unknown)),
                ", ".join(sorted(online)),
            )

    @property
    def client(self) -> GsClient:
        if self._client is None:
            raise RuntimeError("GsClient 未初始化")
        return self._client

    async def download_bytes(self, url: str) -> bytes | None:
        """下载 link:// 或 http(s) 图片（上限 20MB）。"""
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        self.log("warning", "[GsCore] 下载失败 HTTP %s: %s", resp.status, url)
                        return None
                    data = await resp.content.read(MAX_DOWNLOAD_BYTES + 1)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        self.log("warning", "[GsCore] 文件超过 20MB, 已跳过: %s", url)
                        return None
                    return data
        except Exception as e:  # noqa: BLE001
            self.log("warning", "[GsCore] 下载异常 %s: %s", url, e)
            return None

    def read_local_file(self, path: str) -> bytes | None:
        """读取 file:// 本地文件（file:// 指向协议端时通常读不到）。"""
        from pathlib import Path

        try:
            file = Path(path)
            if not file.is_file():
                return None
            data = file.read_bytes()
            if len(data) > MAX_DOWNLOAD_BYTES:
                self.log("warning", "[GsCore] 本地文件超过 20MB, 已跳过: %s", path)
                return None
            return data
        except Exception as e:  # noqa: BLE001
            self.log("warning", "[GsCore] 读取本地文件失败 %s: %s", path, e)
            return None


    # ------------------------------------------------------------------
    # 会话定位（供下行分发使用）
    # ------------------------------------------------------------------
    def lookup_stream_by_msg_id(self, msg_id: str) -> str | None:
        """按上报时的 msg_id（即 stream_id）回查原会话。"""
        return self._msg_stream_map.get(msg_id)

    def _remember_stream(self, msg_id: str, stream_id: str) -> None:
        self._msg_stream_map[msg_id] = stream_id
        if len(self._msg_stream_map) > 500:  # 防止无限增长
            self._msg_stream_map.pop(next(iter(self._msg_stream_map)))

    @staticmethod
    def _extract_stream_id(result: Any) -> str | None:
        """从 chat.get_stream_by_* 的返回结构中递归提取 stream/session id。"""
        if isinstance(result, str):
            return result or None
        if not isinstance(result, dict):
            return None
        for key in ("stream_id", "session_id", "id"):
            value = result.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        for value in result.values():
            found = GscoreAdapterPlugin._extract_stream_id(value)
            if found:
                return found
        return None

    def _lookup_platforms(self) -> list[str]:
        """下行回查时依次尝试的平台名；未限定平台时用 qq（SDK 默认值）。"""
        return sorted(self.target_platforms()) or ["qq"]

    async def find_group_stream(self, group_id: str) -> str | None:
        for platform in self._lookup_platforms():
            key = (platform, group_id)
            cached = self._stream_cache.get(key)
            if cached:
                return cached
            try:
                result = await self.ctx.chat.get_stream_by_group_id(group_id, platform=platform)
                found = self._extract_stream_id(result)
            except Exception as e:  # noqa: BLE001
                self.log("warning", "[GsCore] 查询群会话失败 %s(%s): %s", group_id, platform, e)
                continue
            if found:
                self._stream_cache[key] = found
                return found
        return None

    async def find_private_stream(self, user_id: str) -> str | None:
        for platform in self._lookup_platforms():
            key = (platform, user_id)
            cached = self._stream_cache.get(key)
            if cached:
                return cached
            try:
                result = await self.ctx.chat.get_stream_by_user_id(user_id, platform=platform)
                found = self._extract_stream_id(result)
            except Exception as e:  # noqa: BLE001
                self.log("warning", "[GsCore] 查询私聊会话失败 %s(%s): %s", user_id, platform, e)
                continue
            if found:
                self._stream_cache[key] = found
                return found
        return None

    async def handle_core_packet(self, raw: Any) -> None:
        """下发帧入口（由 GsClient 调用）。"""
        await handle_core_packet(self, raw)


    # ------------------------------------------------------------------
    # 上行转发 Hook：命中前缀 -> 转发 core 并中止 LLM 链路
    # ------------------------------------------------------------------
    @HookHandler(
        "chat.receive.before_process",
        name="gscore_forward",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def forward_to_gscore(self, message: dict[str, Any] | None = None, **kwargs: Any) -> HookResult:
        gs = self.config.gscore
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not gs.only_prefixes and not gs.forward_private:
            return {"action": "continue"}  # 无前缀且私聊不转发，无事可做
        if not (self._client and self._client.is_running):
            return {"action": "continue"}
        if message is None or message.get("is_command") or message.get("is_notify"):
            return {"action": "continue"}

        info = message.get("message_info") or {}
        group_info = info.get("group_info") or {}
        platform = str(message.get("platform") or info.get("platform") or "qq").strip().lower()
        targets = self.target_platforms()
        if targets and platform not in targets:
            return {"action": "continue"}

        bot_self_id = str(gs.bot_self_id or info.get("account_id") or "")
        user_info = info.get("user_info") or {}
        if str(user_info.get("user_id") or "") == bot_self_id:
            return {"action": "continue"}  # 过滤 bot 自身消息，防回环

        is_private = not group_info.get("group_id")
        user_id = str(user_info.get("user_id") or "")

        # 权限规则：屏蔽 QQ / 群聊与私聊黑白名单（被拒消息不拦截，交由麦麦原生处理）
        allowed, deny_reason = check_forward(
            self, user_id=user_id, group_id=str(group_info.get("group_id") or "") or None
        )
        if not allowed:
            self.log("debug", "[GsCore] 消息被权限规则拒绝: %s", deny_reason)
            return {"action": "continue"}

        text = extract_text(message)
        prefix = match_prefix(text, gs.only_prefixes, gs.strict_prefix)
        if not prefix and not (is_private and gs.forward_private):
            return {"action": "continue"}

        if not self._client.is_connected:
            # 已命中前缀但 core 未连接：不拦截，交给 MaiBot 原有流程，避免消息凭空消失
            self.log("warning", "[GsCore] 未连接到 core, 消息未转发: %s", text[:50])
            return {"action": "continue"}

        msg = build_message_receive(self, message, bot_self_id)
        if msg is None:
            return {"action": "continue"}

        stream_id = str(message.get("session_id") or "")
        if stream_id:
            self._remember_stream(msg.msg_id or stream_id, stream_id)
        await self._client.report(msg)
        self.log("info", "【转发】%s[%s]: %s", platform, msg.user_type, text[:80])
        # 中止该消息的后续处理（LLM 闲聊 / 意图识别等）
        return {"action": "abort", "custom_result": {"reason": "GsCore 适配器已接管该消息"}}


    # ------------------------------------------------------------------
    # 管理命令
    # ------------------------------------------------------------------
    def _get_user_id(self, message: dict[str, Any] | None) -> str:
        """从命令消息 dict 中提取发送者 QQ 号。"""
        info = (message or {}).get("message_info") or {}
        return str((info.get("user_info") or {}).get("user_id") or "")

    def _deny_if_not_admin(self, message: dict[str, Any] | None) -> str | None:
        """管理员校验：未配置管理员列表时不限制；非管理员返回提示文本。"""
        if is_admin(self, self._get_user_id(message)):
            return None
        return "该命令仅限管理员使用，请在插件配置 permission.admin_users 中添加你的 QQ 号。"

    @Command("gscore_connect", description="手动重连 gsuid_core", pattern=r"^/?(连接|链接|重连)(早柚)?(core|gscore|核心)$")
    async def cmd_reconnect(self, message: dict[str, Any] | None = None, **kwargs: Any) -> tuple[bool, str, bool]:
        denied = self._deny_if_not_admin(message)
        if denied:
            return True, denied, False
        if self._client is None:
            self._client = GsClient(self)
        await self._client.restart()
        return True, "已重新连接 gsuid_core，请留意日志中的连接结果。", False

    @Command("gscore_status", description="查看 gsuid_core 连接状态", pattern=r"^/?(早柚)?(core|gscore|核心)(状态|连接状态)$")
    async def cmd_status(self, message: dict[str, Any] | None = None, **kwargs: Any) -> tuple[bool, str, bool]:
        denied = self._deny_if_not_admin(message)
        if denied:
            return True, denied, False
        if self._client is None:
            return True, "GsCore 适配器尚未初始化。", False
        state = "已连接" if self._client.is_connected else ("连接中/重连中" if self._client.is_running else "已停止")
        return True, (
            f"GsCore 连接状态：{state}\n"
            f"地址：{self._client.ws_url()}\n"
            f"平台 ID：{self.config.gscore.bot_id}\n"
            f"触发前缀：{' '.join(self.config.gscore.only_prefixes)}"
        ), False

    # ------------------------------------------------------------------
    # WebUI 配置页状态控件
    # ------------------------------------------------------------------
    async def get_config_widget_state(self) -> dict[str, ConfigWidgetState]:
        if self._client is None:
            state = ConfigWidgetState(text="未初始化", tone="neutral", visible=False)
        elif self._client.is_connected:
            state = ConfigWidgetState(text="已连接", tone="success")
        elif self._client.is_running:
            state = ConfigWidgetState(
                text=f"重连中（第 {self._client.retry_count} 次）", tone="warning"
            )
        else:
            state = ConfigWidgetState(text="已停止", tone="error")
        return {"gscore_status": state}


def create_plugin() -> GscoreAdapterPlugin:
    """插件工厂函数（Runner 入口）。"""
    return GscoreAdapterPlugin()
