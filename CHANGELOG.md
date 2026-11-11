# 更新日志

## 1.0.0 (2026-11-11)

首个版本。

- WebSocket 长连接 gsuid_core：断线自动重连（间隔/次数可配）、断连期间消息入队补发、启停幂等
- 上行：Hook `chat.receive.before_process` 拦截命中前缀的群聊/私聊消息，上报 text / image / at / reply 段；图片支持 base64 与直链两种形态
- 下行：text / image（`base64://`、`link://`、`file://` 三形态）/ at / markdown / node（合并转发拆条分发）；`log_{LEVEL}` 包回显日志；`echo` 非空回执 `recall_message_id`；撤回/禁言等控制包短路
- 权限控制（`permission` 配置节）：私聊/群聊黑白名单、屏蔽 QQ、管理员 QQ；管理员命令校验，上报 core 权限至少 pm=3；被拒消息交回麦麦原生流程
- 监听平台多选下拉（qq / telegram / weixin_clawbot / internal）+ `custom_platforms` 兜底第三方平台名；大小写与空白不敏感；启动时探测在线平台并对无效配置告警
- WebUI：配置表单自动生成（基础 / GsCore 连接 / 权限控制），连接状态控件（已连接 / 重连中 / 已停止），配置热重载自动重连
- 管理命令：`/连接core`、`/core状态`
