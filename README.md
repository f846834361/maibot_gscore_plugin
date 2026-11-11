# maibot_gscore_plugin

MaiBot 对接 [gsuid_core（早柚核心）](https://github.com/Genshin-bots/gsuid_core) 的插件。
把命中前缀（如 `gs` / `sr` / `zzz`）的 MaiBot 消息经 WebSocket 转发给 gsuid_core 处理，
并把 core 的回复（文本 / 图片 / 合并转发）发回原 QQ 会话；命中前缀的消息**不会**进入
MaiBot 的 LLM 闲聊链路。

> 实现形态：MaiBot **插件**（`integration` 外部集成），而非 MaiBot 语义的平台适配器——
> gsuid_core 不是聊天平台，它挂在已有 QQ 适配器（NapCat 等）后面。
> 对 gsuid_core 生态而言，本插件扮演"GsCore 适配器"角色，协议对齐官方适配器开发指南。

## 功能

- WebSocket 长连接（`ws://{host}:{port}/ws/{route_id}?token=...`），断线自动重连、断连期间消息暂存补发
- 上行：Hook `chat.receive.before_process` 拦截命中前缀的群聊/私聊消息，构造 `MessageReceive` 上报（支持文本 / 图片 / @ / 引用）
- 下行：解析 `MessageSend`，支持 `text` / `image`（`base64://`、`link://`、`file://` 三形态）/ `at` / `markdown` / `node`（合并转发拆条）；`log_{LEVEL}` 日志包回显到 MaiBot 日志；`echo` 非空自动回执 `recall_message_id`
- 权限控制：私聊/群聊黑白名单、屏蔽 QQ、管理员 QQ；管理员可执行管理命令，上报 core 权限至少 pm=3
- 监听平台多选（qq / telegram / weixin_clawbot / internal）+ 自定义平台名兜底；启动与热重载时核对在线平台，配置错误会告警
- 管理命令：`/连接core` 重连、`/core状态` 查看状态
- WebUI 配置页：基础 / GsCore 连接 / 权限控制三组表单 + 连接状态控件，配置修改热重载

## 安装

### 方式一：WebUI 上传 ZIP（推荐）

1. 打开 MaiBot WebUI → 插件管理 → 上传 / 安装 ZIP，选择 `maibot_gscore_plugin-1.0.0.zip`；
2. 安装成功提示「请重启麦麦使其生效」后重启容器；
3. 安装目录为 `plugins/gscore-plugin`（目录名取自 manifest 的 `id`）。

> 若提示「插件 gscore-plugin 已安装」，需先在插件管理里卸载旧版再上传。

### 方式二：手动复制目录

1. 把本目录复制到 MaiBot 的插件目录：

```text
MaiBot/
└── plugins/
    └── maibot_gscore_plugin/
        ├── plugin.py
        ├── _manifest.json
        ├── models.py
        ├── gs_client.py
        ├── uplink.py
        ├── downlink.py
        ├── segments.py
        └── permissions.py
```

2. 依赖：MaiBot 1.4.x + maibot-plugin-sdk >= 2.22.0（MaiBot 已内置 aiohttp，无需额外安装）。

3. 重启 MaiBot（或通过 WebUI 重载插件），Runner 会依据 `_manifest.json` 与
   `config_model` 自动校验配置并生成 `config.toml`。

## gsuid_core 侧配置

在 gsuid_core 配置目录 `gsuid_config/` 中确保：

1. `[CORE]` 的 `HOST` / `PORT` 与插件配置一致（默认 `8765`）；
   core 与 MaiBot 不在同一台机器/容器时，`HOST` 需监听 `0.0.0.0`。
2. 存在名为 **`maibot`** 的平台配置文件（如 `gsuid_config/maibot.toml`），
   与插件配置的 `bot_id` 保持一致：

```toml
enable = true
HOST = "0.0.0.0"
PORT = "8765"
token = ""   # 设置了则插件侧 ws_token 填同样的值
```

## 插件配置（config.toml 自动生成）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 是否启用转发 |
| `gscore.host` | `127.0.0.1` | core WebSocket 地址（Docker 部署填容器可达地址） |
| `gscore.port` | `8765` | core WebSocket 端口 |
| `gscore.route_id` | `MaiBot` | 连接路由 ID（URL 路径 `/ws/{route_id}`） |
| `gscore.bot_id` | `maibot` | 平台 ID，需与 core 侧平台配置一致 |
| `gscore.bot_self_id` | `""` | 机器人 QQ 号，留空自动取消息账号 |
| `gscore.ws_token` | `""` | core 侧 token，未启用鉴权留空 |
| `gscore.reconnect_interval` | `5` | 断线重连间隔（秒） |
| `gscore.max_retry` | `0` | 最大重试次数，0 = 无限 |
| `gscore.only_prefixes` | `["gs","sr","zzz","ww","mys"]` | 触发转发的命令前缀 |
| `gscore.strict_prefix` | `false` | 严格前缀模式（前缀后须空白/结束） |
| `gscore.forward_private` | `true` | 私聊免前缀全量转发 |
| `gscore.platforms` | `["qq"]` | 监听平台（**多选下拉**：qq / telegram / weixin_clawbot / internal），空 = 不限 |
| `gscore.custom_platforms` | `[]` | 自定义平台名（第三方适配器声明的 platform 值），与多选结果合并 |
| `gscore.user_pm` | `6` | 上报用户权限（2 群主 / 3 管理 / 6 普通） |

### 权限控制（permission 配置节）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `permission.admin_users` | `[]` | 管理员 QQ 号：可使用 `/连接core`、`/core状态` 管理命令，上报 core 权限至少为管理（pm=3）；**留空 = 不限制** |
| `permission.blocked_users` | `[]` | 屏蔽 QQ 号：私聊/群聊一律不转发（最高优先级） |
| `permission.group_whitelist` | `[]` | 群聊白名单（群号）：留空 = 所有群可用 |
| `permission.group_blacklist` | `[]` | 群聊黑名单（群号）：命中的群不转发 |
| `permission.private_whitelist` | `[]` | 私聊白名单（QQ 号）：留空 = 所有私聊可用 |
| `permission.private_blacklist` | `[]` | 私聊黑名单（QQ 号）：命中的用户私聊不转发 |

规则优先级：**屏蔽 QQ > 黑名单 > 白名单**（黑名单与白名单同时配置时黑名单优先；白名单留空表示不限制）。被拒绝的消息不转发也不拦截，仍由麦麦原生流程处理，不会"已读不回"。

## 测试

1. 启动 gsuid_core，看到 `Bot[xxx]连接成功...` 字样。
2. 启动 MaiBot，日志出现 `与[gsuid-core]成功连接! 路由ID: MaiBot`。
3. 在 WebUI 插件配置页查看「GsCore 连接」状态控件，应为绿色「已连接」；或在聊天里发 `/core状态`。
4. 群里发送 `gs帮助`（或已启用适配器对应前缀的命令），应收到 core 的图文回复，且麦麦不再进行闲聊回复。
5. 私聊机器人发送 `帮助`（`forward_private: true` 时免前缀）验证私聊链路。
